import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, status

from app.authorization.models import ProjectAccessContext
from app.config import Settings

logger = logging.getLogger(__name__)
_REQUIRED_GRAPH_APPLICATION_PERMISSION = "CustomSecAttributeAssignment.Read.All"


class GraphProjectAccessReader:
    """Reads server-controlled project authorization attributes from Microsoft Graph."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None, access_store=None) -> None:
        self._settings = settings
        self._http_client = http_client
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._access_store = access_store

    async def read(self, user_object_id: str) -> ProjectAccessContext:
        if not self._is_configured:
            logger.warning(
                "project_authorization_not_configured user_id=%s missing_graph_client_credential=true",
                user_object_id,
            )
            return ProjectAccessContext(projects=(), project_roles={})

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=10.0)
        try:
            token = await self._get_access_token()
            response = await self._read_user(client, user_object_id, token)
            if response.status_code == status.HTTP_401_UNAUTHORIZED:
                logger.warning(
                    "graph_application_token_rejected user_id=%s retrying_with_fresh_token=true",
                    user_object_id,
                )
                await self._invalidate_access_token(token)
                token = await self._get_access_token()
                response = await self._read_user(client, user_object_id, token)
            response.raise_for_status()
            context = parse_project_access(
                response.json().get("customSecurityAttributes"),
                self._settings.entra_custom_attribute_set,
            )
            if self._access_store is not None:
                await self._access_store.synchronize_access(user_object_id, context)
            logger.info(
                "project_authorization_resolved user_id=%s project_count=%s",
                user_object_id,
                len(context.projects),
            )
            return context
        except (httpx.HTTPError, TypeError, ValueError) as error:
            logger.exception("project_authorization_lookup_failed user_id=%s", user_object_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Project authorization information is temporarily unavailable.",
            ) from error
        finally:
            if owns_client:
                await client.aclose()

    @property
    def _is_configured(self) -> bool:
        return bool(
            self._settings.entra_tenant_id
            and self._settings.resolved_entra_client_id
            and self._settings.entra_client_secret
        )

    async def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._access_token_expires_at:
            return self._access_token

        async with self._token_lock:
            if self._access_token and time.time() < self._access_token_expires_at:
                return self._access_token

            owns_client = self._http_client is None
            client = self._http_client or httpx.AsyncClient(timeout=10.0)
            try:
                response = await client.post(
                    (
                        f"{self._settings.entra_authority_host.rstrip('/')}"
                        f"/{self._settings.entra_tenant_id}/oauth2/v2.0/token"
                    ),
                    data={
                        "client_id": self._settings.resolved_entra_client_id,
                        "client_secret": self._settings.entra_client_secret,
                        "grant_type": "client_credentials",
                        "scope": self._settings.graph_scope,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                token = payload.get("access_token")
                if not isinstance(token, str) or not token:
                    raise ValueError("Microsoft identity platform returned no access token.")
                granted_permissions = _application_permissions(token)
                if _REQUIRED_GRAPH_APPLICATION_PERMISSION not in granted_permissions:
                    logger.error(
                        "graph_application_permission_missing required=%s granted=%s",
                        _REQUIRED_GRAPH_APPLICATION_PERMISSION,
                        sorted(granted_permissions),
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Microsoft Graph application permissions are not configured.",
                    )
                expires_in = int(payload.get("expires_in", 3600))
                self._access_token = token
                # OAuth expiry is wall-clock based. A monotonic clock may pause while a laptop
                # sleeps, leaving an already-expired Graph token looking fresh after wake.
                self._access_token_expires_at = time.time() + max(1, expires_in - 60)
                return token
            except (httpx.HTTPError, TypeError, ValueError) as error:
                logger.exception("graph_application_token_request_failed")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Project authorization information is temporarily unavailable.",
                ) from error
            finally:
                if owns_client:
                    await client.aclose()

    async def _invalidate_access_token(self, rejected_token: str) -> None:
        """Invalidate only the token rejected by Graph, preserving concurrent refreshes."""

        async with self._token_lock:
            if self._access_token == rejected_token:
                self._access_token = None
                self._access_token_expires_at = 0.0

    async def _read_user(
        self,
        client: httpx.AsyncClient,
        user_object_id: str,
        token: str,
    ) -> httpx.Response:
        return await client.get(
            f"{self._settings.graph_base_url.rstrip('/')}/users/{user_object_id}",
            params={"$select": "customSecurityAttributes"},
            headers={"Authorization": f"Bearer {token}"},
        )


def parse_project_access(value: Any, attribute_set_name: str) -> ProjectAccessContext:
    if not isinstance(value, Mapping):
        return ProjectAccessContext(projects=(), project_roles={})
    attribute_set = value.get(attribute_set_name)
    if not isinstance(attribute_set, Mapping):
        return ProjectAccessContext(projects=(), project_roles={})

    projects = tuple(_string_values(attribute_set.get("Projects")))
    roles: dict[str, str] = {}
    for assignment in _string_values(attribute_set.get("ProjectRoles")):
        project_id, separator, role = assignment.partition(":")
        project_id = project_id.strip()
        role = role.strip()
        if separator and project_id and role:
            roles[project_id] = role
    return ProjectAccessContext(projects=projects, project_roles=roles)


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _application_permissions(token: str) -> set[str]:
    try:
        claims = jwt.decode(
            token,
            options={"verify_signature": False, "verify_aud": False},
        )
    except jwt.PyJWTError as error:
        raise ValueError("Microsoft Graph returned a malformed access token.") from error
    roles = claims.get("roles", [])
    if not isinstance(roles, list):
        return set()
    return {role for role in roles if isinstance(role, str) and role}

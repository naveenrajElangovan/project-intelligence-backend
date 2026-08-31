import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, status

from app.auth.models import EntraPrincipal
from app.config import Settings


class EntraTokenValidator:
    """Validates access tokens issued for the Project Intelligence API."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http_client = http_client
        self._keys: dict[str, Any] = {}
        self._keys_expire_at = 0.0
        self._refresh_lock = asyncio.Lock()

    async def validate(self, token: str) -> EntraPrincipal:
        self._ensure_configured()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise _invalid_token() from error

        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise _invalid_token()

        key = await self._get_key(header["kid"])
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._settings.entra_audience,
                issuer=self._settings.resolved_entra_issuer,
                options={"require": ["aud", "exp", "iat", "iss", "nbf", "scp", "sub", "tid"]},
            )
        except jwt.PyJWTError as error:
            raise _invalid_token() from error

        if claims.get("tid") != self._settings.entra_tenant_id:
            raise _invalid_token()

        scopes = set(str(claims.get("scp", "")).split())
        if self._settings.entra_required_scope not in scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The access token does not contain the required API scope.",
            )

        object_id = claims.get("oid")
        if not isinstance(object_id, str) or not object_id:
            raise _invalid_token()

        username = _first_string(claims, "preferred_username", "upn", "email") or object_id
        email = _first_string(claims, "email", "preferred_username", "upn")
        return EntraPrincipal(
            object_id=object_id,
            subject=str(claims["sub"]),
            tenant_id=str(claims["tid"]),
            display_name=_first_string(claims, "name") or username,
            username=username,
            email=email,
            claims=dict(claims),
        )

    def _ensure_configured(self) -> None:
        if not self._settings.entra_tenant_id or not self._settings.entra_audience:
            raise RuntimeError("PI_ENTRA_TENANT_ID and PI_ENTRA_AUDIENCE must be configured.")

    async def _get_key(self, key_id: str) -> Any:
        if time.monotonic() >= self._keys_expire_at or key_id not in self._keys:
            await self._refresh_keys()
        key = self._keys.get(key_id)
        if key is None:
            raise _invalid_token()
        return key

    async def _refresh_keys(self) -> None:
        async with self._refresh_lock:
            if time.monotonic() < self._keys_expire_at and self._keys:
                return
            owns_client = self._http_client is None
            client = self._http_client or httpx.AsyncClient(timeout=10.0)
            try:
                metadata_response = await client.get(self._settings.entra_openid_configuration_url)
                metadata_response.raise_for_status()
                metadata = metadata_response.json()
                if metadata.get("issuer", "").rstrip("/") != self._settings.resolved_entra_issuer:
                    raise RuntimeError("Entra OpenID metadata returned an unexpected issuer.")
                jwks_response = await client.get(metadata["jwks_uri"])
                jwks_response.raise_for_status()
                keys = jwks_response.json().get("keys", [])
                self._keys = {
                    item["kid"]: jwt.PyJWK.from_dict(item).key
                    for item in keys
                    if isinstance(item, dict) and isinstance(item.get("kid"), str)
                }
                self._keys_expire_at = time.monotonic() + self._settings.entra_jwks_cache_seconds
            except (httpx.HTTPError, jwt.PyJWTError, KeyError, RuntimeError, TypeError, ValueError) as error:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Identity provider signing keys are temporarily unavailable.",
                ) from error
            finally:
                if owns_client:
                    await client.aclose()


def _first_string(claims: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _invalid_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="The access token is invalid or expired.",
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )

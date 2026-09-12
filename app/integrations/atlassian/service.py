"""Internal client for the read-only Atlassian MCP/event boundary."""

from datetime import datetime

import httpx

from app.config import Settings


class AtlassianServiceClient:
    def __init__(self, settings: Settings):
        self._url = settings.atlassian_service_url.rstrip("/")
        self._key = settings.atlassian_service_internal_api_key

    @property
    def configured(self) -> bool:
        return bool(self._url and self._key)

    async def register_user_session(
        self,
        *,
        user_id: str,
        project_id: str,
        access_token: str,
        expires_at: datetime | None,
    ) -> bool:
        if not self.configured:
            return False
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.put(
                f"{self._url}/v1/internal/sessions",
                headers={"X-Internal-Api-Key": self._key},
                json={
                    "userId": user_id,
                    "projectId": project_id,
                    "accessToken": access_token,
                    "expiresAt": expires_at.isoformat() if expires_at else None,
                },
            )
        response.raise_for_status()
        return True

    async def health(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{self._url}/v1/health")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Atlassian service returned malformed health data.")
        return payload


async def register_session_if_configured(
    settings: Settings, *, user_id: str, project_id: str, access_token: str, expires_at: datetime | None
) -> None:
    client = AtlassianServiceClient(settings)
    if not client.configured:
        return
    try:
        await client.register_user_session(
            user_id=user_id, project_id=project_id, access_token=access_token, expires_at=expires_at
        )
    except httpx.HTTPError:
        # The encrypted backend connection is durable; the MCP session is an
        # ephemeral optimization and can be restored on a later refresh.
        return

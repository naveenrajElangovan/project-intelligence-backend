import asyncio

import httpx

from app.config import Settings
from app.integrations.atlassian.service import AtlassianServiceClient


def test_registers_project_qualified_user_session(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def put(self, url, headers, json):
            captured.update({"url": url, "headers": headers, "json": json})
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    settings = Settings(
        _env_file=None,
        atlassian_service_url="http://atlassian:8000",
        atlassian_service_internal_api_key="internal-secret",
    )

    result = asyncio.run(
        AtlassianServiceClient(settings).register_user_session(
            user_id="user-1",
            project_id="project-1",
            access_token="oauth-token",
            expires_at=None,
        )
    )

    assert result is True
    assert captured["json"] == {
        "userId": "user-1",
        "projectId": "project-1",
        "accessToken": "oauth-token",
        "expiresAt": None,
    }
    assert captured["headers"] == {"X-Internal-Api-Key": "internal-secret"}

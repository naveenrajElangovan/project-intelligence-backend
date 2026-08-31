import asyncio

import httpx
import jwt
import pytest
from fastapi import HTTPException

from app.authorization.graph import GraphProjectAccessReader, parse_project_access
from app.config import Settings


def test_parse_project_access_requires_matching_project_and_role() -> None:
    context = parse_project_access(
        {
            "ProjectIntelligence": {
                "Projects@odata.type": "#Collection(String)",
                "Projects": ["AAOS"],
                "ProjectRoles@odata.type": "#Collection(String)",
                "ProjectRoles": ["AAOS:TECHNICAL_LEAD"],
            }
        },
        "ProjectIntelligence",
    )

    assert context.assignments == (("AAOS", "TECHNICAL_LEAD"),)


def test_graph_reader_uses_app_token_and_reads_current_user_attributes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": _graph_token("CustomSecAttributeAssignment.Read.All"),
                    "expires_in": 3600,
                },
            )
        return httpx.Response(
            200,
            json={
                "customSecurityAttributes": {
                    "ProjectIntelligence": {
                        "Projects": ["AAOS"],
                        "ProjectRoles": ["AAOS:DEVELOPER"],
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        entra_tenant_id="tenant-id",
        entra_audience="api-client-id",
        entra_client_secret="secret",
    )
    reader = GraphProjectAccessReader(settings, client)
    try:
        context = asyncio.run(reader.read("user-object-id"))
    finally:
        asyncio.run(client.aclose())

    assert context.assignments == (("AAOS", "DEVELOPER"),)
    assert requests[1].headers["Authorization"].startswith("Bearer ")
    assert requests[1].url.params["$select"] == "customSecurityAttributes"


def test_graph_reader_rejects_missing_custom_attribute_application_permission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth2/v2.0/token")
        return httpx.Response(
            200,
            json={"access_token": _graph_token("User.Read.All"), "expires_in": 3600},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        entra_tenant_id="tenant-id",
        entra_audience="api-client-id",
        entra_client_secret="secret",
    )
    reader = GraphProjectAccessReader(settings, client)
    try:
        with pytest.raises(HTTPException) as exception:
            asyncio.run(reader.read("user-object-id"))
    finally:
        asyncio.run(client.aclose())

    assert exception.value.status_code == 503
    assert exception.value.detail == "Microsoft Graph application permissions are not configured."


def test_graph_reader_refreshes_once_when_graph_rejects_cached_token() -> None:
    token_requests = 0
    graph_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, graph_requests
        if request.url.path.endswith("/oauth2/v2.0/token"):
            token_requests += 1
            return httpx.Response(
                200,
                json={
                    "access_token": _graph_token(
                        "CustomSecAttributeAssignment.Read.All", f"generation-{token_requests}"
                    ),
                    "expires_in": 3600,
                },
            )
        graph_requests += 1
        if graph_requests == 1:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return httpx.Response(
            200,
            json={
                "customSecurityAttributes": {
                    "ProjectIntelligence": {
                        "Projects": ["DEMO"],
                        "ProjectRoles": ["DEMO:TECHNICAL_LEAD"],
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        entra_tenant_id="tenant-id",
        entra_audience="api-client-id",
        entra_client_secret="secret",
    )
    reader = GraphProjectAccessReader(settings, client)
    try:
        context = asyncio.run(reader.read("user-object-id"))
    finally:
        asyncio.run(client.aclose())

    assert context.assignments == (("DEMO", "TECHNICAL_LEAD"),)
    assert token_requests == 2
    assert graph_requests == 2


def _graph_token(*roles: str) -> str:
    return jwt.encode({"roles": list(roles)}, key="", algorithm="none")

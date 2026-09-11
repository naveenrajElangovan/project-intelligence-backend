import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.internal_ingestion import _validate_atlassian_target, _validate_jira_scope
from app.api.internal_ingestion import proxy_atlassian_read
from app.config import get_settings
from app.integrations.dependencies import get_integration_store
from app.main import app
from app.projects.dependencies import get_project_store
from app.projects.models import JiraProjectSource
from tests.test_internal_ingestion import PROJECT, Projects, Integrations, _settings


@pytest.mark.parametrize(
    "path",
    [
        "field",
        "issue/T0-1",
        "issue/123/comment",
        "issue/123/changelog",
        "issue/T0-1/worklog",
        "issue/T0-1/remotelink",
    ],
)
def test_allowed_jira_read_paths(path):
    _validate_atlassian_target(
        "https://api.atlassian.com/ex/jira/cloud/rest/api/3/" + path, "cloud"
    )


@pytest.mark.parametrize(
    "path", ["issue/T0-1/delete", "issue/123/comment/../other", "../issue/1", "issue/T0-1?x=1"]
)
def test_disallowed_jira_read_paths(path):
    # Queries are supplied through structured parameters, not embedded targets.
    with pytest.raises(HTTPException):
        _validate_atlassian_target(
            "https://api.atlassian.com/ex/jira/cloud/rest/api/3/" + path, "cloud"
        )


def test_project_discovery_is_authenticated_and_lists_unconnected_mappings():
    project = replace(
        PROJECT, jira_projects=(JiraProjectSource("https://personal.atlassian.net", "T0"),)
    )

    class Store(Projects):
        async def list_active(self):
            return (project,)

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_project_store] = Store
    app.dependency_overrides[get_integration_store] = Integrations
    try:
        client = TestClient(app)
        assert client.get("/v1/internal/ingestion/projects").status_code == 401
        response = client.get(
            "/v1/internal/ingestion/projects", headers={"Authorization": "Bearer " + "i" * 32}
        )
        assert response.status_code == 200
        assert response.json()[0]["jiraProjects"][0]["projectKey"] == "T0"
        assert response.json()[0]["atlassian"] is None
    finally:
        app.dependency_overrides.clear()


def test_gateway_rejects_unmapped_issue_and_attachment():
    project = replace(
        PROJECT, jira_projects=(JiraProjectSource("https://personal.atlassian.net", "T0"),)
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"fields": {"project": {"key": "OTHER"}, "attachment": []}}
                )
            )
        ) as client:
            with pytest.raises(HTTPException):
                await _validate_jira_scope(
                    client,
                    project,
                    "cloud",
                    "https://personal.atlassian.net",
                    "https://api.atlassian.com/ex/jira/cloud/rest/api/3/issue/1",
                    {},
                    "test-token",
                )
            with pytest.raises(HTTPException):
                await _validate_jira_scope(
                    client,
                    project,
                    "cloud",
                    "https://personal.atlassian.net",
                    "https://api.atlassian.com/ex/jira/cloud/rest/api/3/search/jql",
                    {"jql": 'project = "OTHER" ORDER BY updated ASC, key ASC'},
                    "test-token",
                )

    asyncio.run(run())


@pytest.mark.parametrize("key", ["SERVICE_DESK", "HR7", "Z"])
def test_generic_projects_and_site_boundaries(key):
    site = "https://operations.atlassian.net"
    project = replace(
        PROJECT,
        jira_projects=(
            JiraProjectSource(site, key),
            JiraProjectSource("https://other.atlassian.net", "OTHER"),
        ),
    )
    origin = "https://api.atlassian.com/ex/jira/operations-cloud/rest/api/3/"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"fields": {"project": {"key": key}, "attachment": [{"id": "7"}]}}
                )
            )
        ) as client:
            for path, params in [
                (f"issue/{key}-903", {}),
                ("issue/903/comment", {}),
                ("attachment/content/7", {"jira_issue_key": f"{key}-903"}),
                ("search/jql", {"jql": f'project = "{key}" ORDER BY updated ASC, key ASC'}),
            ]:
                _validate_atlassian_target(origin + path, "operations-cloud")
                await _validate_jira_scope(
                    client, project, "operations-cloud", site, origin + path, params, "test-token"
                )
            for path, params in [
                ("attachment/content/8", {"jira_issue_key": f"{key}-903"}),
                ("search/jql", {"jql": 'project = "OTHER" ORDER BY updated ASC, key ASC'}),
            ]:
                with pytest.raises(HTTPException):
                    await _validate_jira_scope(
                        client,
                        project,
                        "operations-cloud",
                        site,
                        origin + path,
                        params,
                        "test-token",
                    )
            with pytest.raises(HTTPException):
                await _validate_jira_scope(
                    client,
                    project,
                    "operations-cloud",
                    "https://other.atlassian.net",
                    origin + "issue/903",
                    {},
                    "test-token",
                )
            with pytest.raises(HTTPException):
                _validate_atlassian_target(origin + "issue/903", "different-cloud")

    asyncio.run(run())


def test_expired_project_credentials_do_not_disable_or_authorize_another_cloud(monkeypatch):
    calls = []

    class ProjectStore:
        async def get(self, project_id):
            return replace(
                PROJECT,
                project_id=project_id,
                jira_projects=(JiraProjectSource(f"https://{project_id}.atlassian.net", "OPS"),),
            )

    class Connections:
        async def get_connection(self, project_id, provider):
            return SimpleNamespace(
                project_id=project_id,
                resource_id=project_id + "-cloud",
                resource_url=f"https://{project_id}.atlassian.net",
            )

    async def validate(connection, *args):
        if connection.project_id == "expired":
            raise ValueError("The Atlassian connection has expired")
        return connection, "test-live-token"

    monkeypatch.setattr("app.api.integrations._valid_atlassian_connection", validate)
    client_type = httpx.AsyncClient

    def respond(request):
        calls.append(str(request.url))
        assert "/ex/jira/healthy-cloud/" in str(request.url)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        "app.api.internal_ingestion.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond)),
    )

    async def read(project_id, cloud):
        return await proxy_atlassian_read(
            project_id,
            Request(
                {"type": "http", "query_string": b"", "headers": [], "client": ("testclient", 1)}
            ),
            f"https://api.atlassian.com/ex/jira/{cloud}/rest/api/3/field",
            "Bearer " + "i" * 32,
            _settings(),
            ProjectStore(),
            Connections(),
            object(),
        )

    async def run():
        results = await asyncio.gather(
            read("expired", "expired-cloud"),
            read("healthy", "healthy-cloud"),
            return_exceptions=True,
        )
        assert isinstance(results[0], HTTPException) and results[0].status_code == 409
        assert results[1].status_code == 200
        with pytest.raises(HTTPException):
            await read("expired", "healthy-cloud")
        assert len(calls) == 1

    asyncio.run(run())

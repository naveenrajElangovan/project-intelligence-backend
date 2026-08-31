from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_principal
from app.auth.models import EntraPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.models import ProjectAccessContext
from app.config import Settings, get_settings
from app.main import app
from app.projects.dependencies import get_project_store
from app.projects.models import (
    ConfluenceSpaceSource,
    GitHubRepositorySource,
    JiraProjectSource,
    VectorStoreRoute,
    ProjectDefinition,
)


def _principal() -> EntraPrincipal:
    return EntraPrincipal(
        object_id="entra-object-id",
        subject="subject-id",
        tenant_id="tenant-id",
        display_name="Developer",
        username="developer@example.com",
        email="developer@example.com",
        claims={},
    )


class _AccessReader:
    def __init__(self, projects: tuple[str, ...], roles: dict[str, str]) -> None:
        self._context = ProjectAccessContext(projects=projects, project_roles=roles)

    async def read(self, user_object_id: str) -> ProjectAccessContext:
        assert user_object_id == "entra-object-id"
        return self._context


class _ProjectStore:
    async def get(self, project_id: str):
        return ProjectDefinition(
            project_id="AAOS",
            display_name="AAOS",
            active=True,
            jira_projects=(JiraProjectSource("https://example.atlassian.net", "AAOS"),),
            confluence_spaces=(
                ConfluenceSpaceSource("https://example.atlassian.net", "AAOS", "163844"),
            ),
            github_repositories=(GitHubRepositorySource("owner", "repo", ("main",)),),
            vector_store=VectorStoreRoute("project-intelligence"),
        )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        atlassian_client_id="atlassian-client",
        atlassian_client_secret="atlassian-secret",
        provider_token_encryption_key="encryption-key",
    )


def test_dashboard_returns_authorized_project_contract() -> None:
    app.dependency_overrides[get_current_principal] = _principal
    app.dependency_overrides[get_graph_project_access_reader] = lambda: _AccessReader(
        projects=("AAOS",),
        roles={"AAOS": "TECHNICAL_LEAD"},
    )
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_project_store] = _ProjectStore
    try:
        response = TestClient(app).get(
            "/v1/projects/AAOS/dashboard",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["project"] == {
        "projectId": "AAOS",
        "name": "AAOS",
        "role": "TECHNICAL_LEAD",
        "health": "UNKNOWN",
        "progress": 0,
        "currentStage": "NOT_SYNCHRONIZED",
        "releaseTarget": "Not set",
        "lastSynchronizedAt": "Never",
    }
    assert body["integrations"] == []
    assert "not connected from this application" in body["statusSummary"]


def test_dashboard_returns_403_for_unassigned_project() -> None:
    app.dependency_overrides[get_current_principal] = _principal
    app.dependency_overrides[get_graph_project_access_reader] = lambda: _AccessReader(
        projects=(),
        roles={},
    )
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_project_store] = _ProjectStore
    try:
        response = TestClient(app).get(
            "/v1/projects/AAOS/dashboard",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json() == {"detail": "You do not have access to this project."}

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.integrations.dependencies import get_integration_store
from app.integrations.models import ProviderConnection
from app.main import app
from app.projects.dependencies import get_project_store
from app.projects.models import (
    ConfluenceSpaceSource,
    GitHubRepositorySource,
    IngestionSchedule,
    JiraProjectSource,
    VectorStoreRoute,
    ProjectDefinition,
)


PROJECT = ProjectDefinition(
    project_id="DEMO",
    display_name="DEMO",
    active=True,
    jira_projects=(),
    confluence_spaces=(),
    github_repositories=(
        GitHubRepositorySource("personal-owner", "private-repo", ("main",)),
    ),
    vector_store=VectorStoreRoute("project-intelligence", "chunk_text"),
    ingestion_schedule=IngestionSchedule(),
)


class Projects:
    async def get(self, project_id: str):
        return PROJECT if project_id == PROJECT.project_id else None

    async def find_by_github_repository(self, owner: str, repository: str):
        if (owner, repository) == ("personal-owner", "private-repo"):
            return PROJECT, PROJECT.github_repositories[0]
        return None


class Integrations:
    async def get_connection(self, project_id: str, provider: str) -> ProviderConnection | None:
        return None


def _settings() -> Settings:
    return Settings(_env_file=None, ingestion_internal_api_key="i" * 32)


def test_internal_project_mapping_requires_service_credential() -> None:
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_project_store] = Projects
    app.dependency_overrides[get_integration_store] = Integrations
    try:
        response = TestClient(app).get("/v1/internal/ingestion/projects/DEMO")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401


def test_internal_project_mapping_contains_routing_but_no_tokens() -> None:
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_project_store] = Projects
    app.dependency_overrides[get_integration_store] = Integrations
    try:
        response = TestClient(app).get(
            "/v1/internal/ingestion/projects/DEMO",
            headers={"Authorization": f"Bearer {'i' * 32}"},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    payload = response.json()
    assert payload["projectId"] == "DEMO"
    assert payload["githubRepositories"][0]["repository"] == "private-repo"
    assert payload["sourceAccessRules"] == []
    assert payload["retrievalProfile"] is None
    assert "accessToken" not in response.text
    assert "refreshToken" not in response.text


def test_internal_project_mapping_does_not_require_atlassian_connection() -> None:
    project = ProjectDefinition(
        project_id="DEMO",
        display_name="DEMO",
        active=True,
        jira_projects=(JiraProjectSource("https://example.atlassian.net", "T0"),),
        confluence_spaces=(
            ConfluenceSpaceSource("https://example.atlassian.net", "T20", "2916360"),
        ),
        github_repositories=PROJECT.github_repositories,
        vector_store=PROJECT.vector_store,
        ingestion_schedule=PROJECT.ingestion_schedule,
    )

    class AtlassianProjects(Projects):
        async def get(self, project_id: str):
            return project if project_id == project.project_id else None

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_project_store] = AtlassianProjects
    app.dependency_overrides[get_integration_store] = Integrations
    try:
        response = TestClient(app).get(
            "/v1/internal/ingestion/projects/DEMO",
            headers={"Authorization": f"Bearer {'i' * 32}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["atlassian"] is None

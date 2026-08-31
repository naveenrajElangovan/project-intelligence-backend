import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.api import integrations as integration_api
from app.auth.dependencies import get_current_principal
from app.auth.models import EntraPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.models import ProjectAccessContext
from app.config import Settings, get_settings
from app.integrations.atlassian.client import AtlassianIdentity, AtlassianResource
from app.integrations.dependencies import get_integration_store, get_secret_store
from app.integrations.models import (
    AtlassianCredentials,
    OAuthState,
    ProjectSource,
    ProviderConnection,
)
from app.main import app
from app.projects.dependencies import get_project_store
from app.projects.models import (
    ConfluenceSpaceSource,
    GitHubRepositorySource,
    JiraProjectSource,
    VectorStoreRoute,
    ProjectDefinition,
)


class FakeStore:
    def __init__(self) -> None:
        self.state: OAuthState | None = None
        self.connection: ProviderConnection | None = None
        self.sources: tuple[ProjectSource, ...] = ()
        self.synchronizations: list[tuple[str, str]] = []

    async def save_oauth_state(self, state: OAuthState) -> None:
        self.state = state

    async def consume_oauth_state(self, state_hash: str, provider: str) -> OAuthState | None:
        if self.state and self.state.state_hash == state_hash and self.state.provider == provider:
            state, self.state = self.state, None
            return state
        return None

    async def upsert_connection(self, connection: ProviderConnection) -> None:
        self.connection = connection

    async def get_connection(
        self, project_id: str, provider: str
    ) -> ProviderConnection | None:
        return self.connection

    async def save_synchronized_sources(
        self,
        project_id: str,
        provider: str,
        sources: tuple[ProjectSource, ...],
        synchronized_at: datetime,
    ) -> None:
        self.synchronizations.append((project_id, provider))
        self.sources += sources

    async def list_authorized_sources(
        self, project_id: str, provider: str | None, limit: int
    ) -> tuple[ProjectSource, ...]:
        return tuple(
            source for source in self.sources if provider is None or source.provider == provider
        )[:limit]


class AccessReader:
    async def read(self, user_object_id: str) -> ProjectAccessContext:
        return ProjectAccessContext(projects=("AAOS",), project_roles={"AAOS": "TECHNICAL_LEAD"})


class NoAccessReader:
    async def read(self, user_object_id: str) -> ProjectAccessContext:
        return ProjectAccessContext(projects=(), project_roles={})


class FakeSecretStore:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, object]] = {}

    async def put_json(self, name: str, value: dict[str, object]) -> str:
        reference = f"local://{name}"
        self.values[reference] = value
        return reference

    async def get_json(self, reference: str) -> dict[str, object]:
        return self.values[reference]

    async def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


class ProjectStore:
    async def get(self, project_id: str):
        return ProjectDefinition(
            project_id="AAOS",
            display_name="AAOS",
            active=True,
            jira_projects=(JiraProjectSource("https://example.atlassian.net", "AAOS"),),
            confluence_spaces=(
                ConfluenceSpaceSource("https://example.atlassian.net", "AAOS", "163844"),
            ),
            github_repositories=(
                GitHubRepositorySource("personal-owner", "private-repo", ("main",)),
            ),
            vector_store=VectorStoreRoute("project-intelligence"),
        )


def principal() -> EntraPrincipal:
    return EntraPrincipal(
        object_id="user-id",
        subject="subject",
        tenant_id="tenant-id",
        display_name="Developer",
        username="developer@example.com",
        email="developer@example.com",
        claims={},
    )


def settings() -> Settings:
    return Settings(
        _env_file=None,
        atlassian_client_id="client-id",
        atlassian_client_secret="client-secret",
        atlassian_redirect_uri="http://localhost:8001/v1/integrations/atlassian/callback",
        provider_token_encryption_key=base64.b64encode(b"a" * 32).decode(),
    )


def configure(store: FakeStore, secret_store: FakeSecretStore | None = None) -> FakeSecretStore:
    secret_store = secret_store or FakeSecretStore()
    app.dependency_overrides[get_current_principal] = principal
    app.dependency_overrides[get_graph_project_access_reader] = AccessReader
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_integration_store] = lambda: store
    app.dependency_overrides[get_project_store] = ProjectStore
    app.dependency_overrides[get_secret_store] = lambda: secret_store
    return secret_store


def test_connect_returns_atlassian_authorization_url_bound_to_user() -> None:
    store = FakeStore()
    configure(store)
    try:
        response = TestClient(app).post(
            "/v1/projects/AAOS/integrations/atlassian/connect",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    query = parse_qs(urlparse(response.json()["authorizationUrl"]).query)
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == [settings().atlassian_redirect_uri]
    assert query["audience"] == ["api.atlassian.com"]
    assert store.state is not None
    assert store.state.user_id == "user-id"
    assert store.state.project_id == "AAOS"


def test_status_returns_shared_jira_and_confluence_connection() -> None:
    store = FakeStore()
    now = datetime.now(UTC)
    secret_store = FakeSecretStore()
    secret_store.values["local://atlassian"] = {
        "accessToken": "token",
        "refreshToken": "refresh",
    }
    store.connection = ProviderConnection(
        connected_by="user-id",
        tenant_id="tenant-id",
        project_id="AAOS",
        provider="ATLASSIAN",
        secret_reference="local://atlassian",
        resource_id="cloud-id",
        resource_url="https://example.atlassian.net",
        resource_name="Example",
        scopes=("read:jira-work", "read:page:confluence", "read:attachment:confluence"),
        token_expires_at=now - timedelta(minutes=1),
        connected_at=now,
        updated_at=now,
    )
    configure(store, secret_store)
    try:
        response = TestClient(app).get(
            "/v1/projects/AAOS/integrations",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert [item["provider"] for item in body] == ["JIRA", "CONFLUENCE", "GITHUB"]
    assert body[0]["connected"] is True
    assert body[1]["connected"] is True
    assert body[0]["resourceUrl"] == "https://example.atlassian.net"


def test_access_context_combines_entra_assignment_and_backend_source_access() -> None:
    store = FakeStore()
    configure(store)
    try:
        response = TestClient(app).get(
            "/v1/projects/AAOS/access-context",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["projectId"] == "AAOS"
    assert body["role"] == "TECHNICAL_LEAD"
    assert body["authorized"] is True
    assert body["authorizationMode"] == "ENTRA_PROJECT_SCOPED"
    assert body["canAskQuestions"] is True
    assert [item["provider"] for item in body["integrations"]] == [
        "JIRA",
        "CONFLUENCE",
        "GITHUB",
    ]
    assert body["integrations"][0]["available"] is False
    assert body["integrations"][2]["available"] is True


def test_access_context_rejects_a_project_not_assigned_by_entra() -> None:
    store = FakeStore()
    configure(store)
    app.dependency_overrides[get_graph_project_access_reader] = NoAccessReader
    try:
        response = TestClient(app).get(
            "/v1/projects/AAOS/access-context",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"] == "You do not have access to this project."


def test_callback_exchanges_code_and_persists_secret_reference(monkeypatch) -> None:
    store = FakeStore()
    raw_state = "oauth-state"
    store.state = OAuthState(
        state_hash=integration_api._state_hash(raw_state),
        user_id="user-id",
        tenant_id="tenant-id",
        project_id="AAOS",
        provider="ATLASSIAN",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    secret_store = configure(store)

    async def exchange_code(self, code: str) -> AtlassianCredentials:
        assert code == "authorization-code"
        return AtlassianCredentials(
            access_token="access-token",
            refresh_token="refresh-token",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=("read:jira-work",),
        )

    async def accessible_resources(self, access_token: str):
        assert access_token == "access-token"
        return (
            AtlassianResource(
                cloud_id="cloud-id",
                url="https://example.atlassian.net",
                name="Example",
                scopes=("read:jira-work",),
            ),
        )

    async def current_user(self, access_token: str, cloud_id: str):
        assert (access_token, cloud_id) == ("access-token", "cloud-id")
        return AtlassianIdentity(
            account_id="atlassian-user-id",
            display_name="Developer",
            email="developer@example.com",
        )

    monkeypatch.setattr(integration_api.AtlassianOAuthClient, "exchange_code", exchange_code)
    monkeypatch.setattr(
        integration_api.AtlassianOAuthClient, "accessible_resources", accessible_resources
    )
    monkeypatch.setattr(integration_api.AtlassianOAuthClient, "current_user", current_user)
    try:
        response = TestClient(app).get(
            "/v1/integrations/atlassian/callback",
            params={"state": raw_state, "code": "authorization-code"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Atlassian connected" in response.text
    assert store.connection is not None
    saved = secret_store.values[store.connection.secret_reference]
    assert saved["accessToken"] == "access-token"
    assert saved["refreshToken"] == "refresh-token"
    assert store.connection.provider_account_id == "atlassian-user-id"


def test_synchronization_uses_current_users_connection_and_stores_access(monkeypatch) -> None:
    store = FakeStore()
    now = datetime.now(UTC)
    secret_store = FakeSecretStore()
    secret_store.values["local://atlassian"] = {
        "accessToken": "user-access-token",
        "refreshToken": "refresh-token",
    }
    store.connection = ProviderConnection(
        connected_by="user-id",
        tenant_id="tenant-id",
        project_id="AAOS",
        provider="ATLASSIAN",
        secret_reference="local://atlassian",
        resource_id="cloud-id",
        resource_url="https://example.atlassian.net",
        resource_name="Example",
        scopes=("read:jira-work", "read:page:confluence", "read:attachment:confluence"),
        token_expires_at=now + timedelta(hours=1),
        connected_at=now,
        updated_at=now,
    )

    configure(store, secret_store)

    async def jira_sources(self, access_token, cloud_id, project_id, project_key, resource_url):
        assert (access_token, cloud_id, project_id, project_key) == (
            "user-access-token",
            "cloud-id",
            "AAOS",
            "AAOS",
        )
        return (_source("JIRA", "AAOS-1", "ISSUE"),)

    async def confluence_sources(self, access_token, cloud_id, project_id, space_id, resource_url):
        assert (access_token, cloud_id, project_id, space_id) == (
            "user-access-token",
            "cloud-id",
            "AAOS",
            "163844",
        )
        return (
            _source("CONFLUENCE", "page:1", "PAGE"),
            _source("CONFLUENCE", "attachment:2", "ATTACHMENT"),
        )

    monkeypatch.setattr(integration_api.AtlassianOAuthClient, "jira_sources", jira_sources)
    monkeypatch.setattr(
        integration_api.AtlassianOAuthClient, "confluence_sources", confluence_sources
    )
    try:
        response = TestClient(app).post(
            "/v1/projects/AAOS/integrations/atlassian/synchronize",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["jiraIssues"] == 1
    assert response.json()["confluencePages"] == 1
    assert response.json()["confluenceAttachments"] == 1
    assert store.synchronizations == [
        ("AAOS", "JIRA"),
        ("AAOS", "CONFLUENCE"),
    ]


def test_sources_returns_only_store_authorized_records() -> None:
    store = FakeStore()
    store.sources = (
        _source("JIRA", "AAOS-1", "ISSUE"),
        _source("CONFLUENCE", "page:1", "PAGE"),
    )
    configure(store)
    try:
        response = TestClient(app).get(
            "/v1/projects/AAOS/sources",
            params={"provider": "confluence"},
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "provider": "CONFLUENCE",
            "sourceId": "page:1",
            "sourceType": "PAGE",
            "title": "page:1",
            "reference": "page:1",
            "sourceUrl": "https://example.test/page:1",
            "updatedAt": None,
        }
    ]


def _source(provider: str, source_id: str, source_type: str) -> ProjectSource:
    return ProjectSource(
        project_id="AAOS",
        provider=provider,
        source_id=source_id,
        source_type=source_type,
        title=source_id,
        reference=source_id,
        source_url=f"https://example.test/{source_id}",
        content="content",
        metadata={},
        source_updated_at=None,
    )

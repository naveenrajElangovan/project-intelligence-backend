from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_principal
from app.auth.models import EntraPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.models import ProjectAccessContext
from app.main import app
from app.projects.dependencies import get_project_store


class FakeStore:
    def __init__(self) -> None:
        self.project = None

    async def get(self, project_id: str):
        return self.project

    async def upsert(self, project) -> None:
        self.project = project


class AccessReader:
    def __init__(self, role: str = "TECHNICAL_LEAD") -> None:
        self.role = role

    async def read(self, user_id: str):
        return ProjectAccessContext(
            projects=("POS_BOT",),
            project_roles={"POS_BOT": self.role},
        )


def principal():
    return EntraPrincipal(
        object_id="user-id",
        subject="subject",
        tenant_id="tenant",
        display_name="Lead",
        username="lead@example.com",
        email="lead@example.com",
        claims={},
    )


def body():
    return {
        "displayName": "POS and BOT",
        "jiraProjects": [
            {"siteUrl": "https://example.atlassian.net", "projectKey": "POSBOT"}
        ],
        "confluenceSpaces": [
            {
                "siteUrl": "https://example.atlassian.net",
                "spaceKey": "POSBOT",
                "spaceId": "12345",
                "rootPageIds": ["100"],
            }
        ],
        "githubRepositories": [
            {
                "owner": "personal-owner",
                "repository": "private-repo",
                "indexedBranches": ["main", "release/2.0"],
                "includePaths": ["coreApp/**"],
                "excludePaths": ["**/build/**"],
            }
        ],
        "vectorStore": {
            "collectionName": "project-intelligence",
            "textField": "chunk_text",
        },
        "ingestionSchedule": {
            "githubMergedPrEnabled": True,
            "dailyEnabled": True,
            "dailyAt": "00:00",
            "timezone": "America/Mexico_City",
            "manualEnabled": True,
        },
    }


def configure(store: FakeStore, role: str = "TECHNICAL_LEAD") -> None:
    app.dependency_overrides[get_current_principal] = principal
    app.dependency_overrides[get_graph_project_access_reader] = lambda: AccessReader(role)
    app.dependency_overrides[get_project_store] = lambda: store


def test_technical_lead_can_store_project_source_mappings() -> None:
    store = FakeStore()
    configure(store)
    try:
        response = TestClient(app).put(
            "/v1/projects/POS_BOT/configuration",
            json=body(),
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["githubRepositories"][0]["owner"] == "personal-owner"
    assert response.json()["vectorStore"]["collectionName"] == "project-intelligence"
    assert response.json()["ingestionSchedule"]["dailyAt"] == "00:00"
    assert store.project.github_repositories[0].repository == "private-repo"


def test_non_lead_cannot_change_project_source_mappings() -> None:
    store = FakeStore()
    configure(store, role="DEVELOPER")
    try:
        response = TestClient(app).put(
            "/v1/projects/POS_BOT/configuration",
            json=body(),
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert store.project is None


def test_accepts_provider_neutral_collection_name() -> None:
    store = FakeStore()
    configure(store)
    updated = body()
    updated["vectorStore"]["collectionName"] = "future-provider-compatible"
    try:
        response = TestClient(app).put(
            "/v1/projects/POS_BOT/configuration",
            json=updated,
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert store.project is not None
    assert store.project.vector_store.collection_name == "future-provider-compatible"


def test_source_access_and_retrieval_configuration_round_trip() -> None:
    store = FakeStore()
    configure(store)
    updated = body()
    updated["sourceAccessRules"] = [
        {
            "provider": "CONFLUENCE",
            "matchField": "TITLE",
            "prefix": "[STORE]",
            "accessPolicyId": "department:POS_BOT:STORE_OPERATIONS",
        }
    ]
    updated["retrievalProfile"] = {
        "maxChunksPerSource": 12,
        "rerankTopN": 16,
        "mixedSourceTopN": 12,
        "rerankScoreThreshold": 0.0,
    }
    try:
        response = TestClient(app).put(
            "/v1/projects/POS_BOT/configuration",
            json=updated,
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["sourceAccessRules"] == updated["sourceAccessRules"]
    assert response.json()["retrievalProfile"] == updated["retrievalProfile"]
    assert store.project.source_access_rules[0].access_policy_id == (
        "department:POS_BOT:STORE_OPERATIONS"
    )
    assert store.project.retrieval_profile.max_chunks_per_source == 12
    assert store.project.retrieval_profile.rerank_score_threshold == 0.0


def test_source_access_rule_cannot_name_another_project() -> None:
    store = FakeStore()
    configure(store)
    updated = body()
    updated["sourceAccessRules"] = [
        {
            "provider": "CONFLUENCE",
            "matchField": "TITLE",
            "prefix": "[STORE]",
            "accessPolicyId": "department:OTHER:STORE_OPERATIONS",
        }
    ]
    try:
        response = TestClient(app).put(
            "/v1/projects/POS_BOT/configuration",
            json=updated,
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert store.project is None

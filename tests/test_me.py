from azure.core.exceptions import ClientAuthenticationError
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError

from app.auth.dependencies import get_current_principal
from app.auth.models import EntraPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.models import ProjectAccessContext
from app.main import app
from app.projects.dependencies import get_project_store
from app.projects.models import ProjectDefinition, VectorStoreRoute


def test_me_uses_server_validated_claims() -> None:
    app.dependency_overrides[get_current_principal] = lambda: EntraPrincipal(
        object_id="entra-object-id",
        subject="subject-id",
        tenant_id="tenant-id",
        display_name="Naveenraj Elangovan",
        username="naveen@example.com",
        email="naveen@example.com",
        claims={
            "employeeid": "EMP-12345",
            "departments": "Technology",
            "projects": "AAOS",
            "project_roles": "AAOS:TECHNICAL_LEAD",
        },
    )
    app.dependency_overrides[get_graph_project_access_reader] = lambda: _FakeAccessReader()
    app.dependency_overrides[get_project_store] = _FakeProjectStore
    try:
        response = TestClient(app).get("/v1/me", headers={"Authorization": "Bearer test"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "userId": "entra-object-id",
        "employeeId": "EMP-12345",
        "username": "naveen@example.com",
        "displayName": "Naveenraj Elangovan",
        "email": "naveen@example.com",
        "departments": ["Technology", "STORE_OPERATIONS"],
        "assignedProjects": [
            {"projectId": "AAOS", "displayName": "AAOS Pilot", "role": "TECHNICAL_LEAD"}
        ],
    }


class _FakeAccessReader:
    async def read(self, user_object_id: str) -> ProjectAccessContext:
        assert user_object_id == "entra-object-id"
        return ProjectAccessContext(
            projects=("AAOS",),
            project_roles={"AAOS": "TECHNICAL_LEAD"},
            project_departments={"AAOS": ("STORE_OPERATIONS",)},
        )


class _FakeProjectStore:
    async def list_by_ids(self, project_ids: tuple[str, ...]):
        return (
            ProjectDefinition(
                project_id="AAOS",
                display_name="AAOS Pilot",
                active=True,
                jira_projects=(),
                confluence_spaces=(),
                github_repositories=(),
                vector_store=VectorStoreRoute("project-intelligence"),
            ),
        )


def test_me_requires_bearer_token() -> None:
    response = TestClient(app).get("/v1/me")

    assert response.status_code == 401


def test_me_reports_project_store_outage_as_service_unavailable() -> None:
    app.dependency_overrides[get_current_principal] = lambda: EntraPrincipal(
        object_id="entra-object-id",
        subject="subject-id",
        tenant_id="tenant-id",
        display_name="Project User",
        username="user@example.com",
        email="user@example.com",
        claims={},
    )
    app.dependency_overrides[get_graph_project_access_reader] = lambda: _FakeAccessReader()
    app.dependency_overrides[get_project_store] = _UnavailableProjectStore
    try:
        response = TestClient(app).get("/v1/me", headers={"Authorization": "Bearer test"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"] == "Project configuration is temporarily unavailable."


class _UnavailableProjectStore:
    async def list_by_ids(self, project_ids: tuple[str, ...]):
        raise OperationalError("SELECT projects", {}, Exception("timeout"))


def test_me_reports_database_identity_outage_as_service_unavailable() -> None:
    app.dependency_overrides[get_current_principal] = lambda: EntraPrincipal(
        object_id="entra-object-id",
        subject="subject-id",
        tenant_id="tenant-id",
        display_name="Project User",
        username="user@example.com",
        email="user@example.com",
        claims={},
    )
    app.dependency_overrides[get_graph_project_access_reader] = lambda: _FakeAccessReader()
    app.dependency_overrides[get_project_store] = _UnavailableIdentityProjectStore
    try:
        response = TestClient(app).get("/v1/me", headers={"Authorization": "Bearer test"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"] == "Project configuration is temporarily unavailable."


class _UnavailableIdentityProjectStore:
    async def list_by_ids(self, project_ids: tuple[str, ...]):
        raise ClientAuthenticationError("No database identity is available.")


@pytest.mark.parametrize(
    "graph_projects",
    (("T2.0", "T2.0-STORE"), ("T2.0-STORE", "T2.0")),
)
def test_me_preserves_graph_project_order_instead_of_display_name_order(
    graph_projects: tuple[str, ...],
) -> None:
    class OrderedAccessReader:
        async def read(self, user_object_id: str) -> ProjectAccessContext:
            return ProjectAccessContext(
                projects=graph_projects,
                project_roles={
                    "T2.0": "TECHNICAL_LEAD",
                    "T2.0-STORE": "STORE_USER",
                },
            )

    class DisplayNameSortedStore:
        async def list_by_ids(self, project_ids: tuple[str, ...]):
            def project(project_id: str, display_name: str) -> ProjectDefinition:
                return ProjectDefinition(
                    project_id=project_id,
                    display_name=display_name,
                    active=True,
                    jira_projects=(),
                    confluence_spaces=(),
                    github_repositories=(),
                    vector_store=VectorStoreRoute("project-intelligence"),
                )

            return (
                project("T2.0-STORE", "Tiendas 2.0 Store Assistant"),
                project("T2.0", "Tiendas 3B Platform"),
            )

    app.dependency_overrides[get_current_principal] = lambda: EntraPrincipal(
        object_id="entra-object-id",
        subject="subject-id",
        tenant_id="tenant-id",
        display_name="Developer",
        username="developer@example.com",
        email="developer@example.com",
        claims={},
    )
    app.dependency_overrides[get_graph_project_access_reader] = lambda: OrderedAccessReader()
    app.dependency_overrides[get_project_store] = DisplayNameSortedStore
    try:
        response = TestClient(app).get("/v1/me", headers={"Authorization": "Bearer test"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [
        assignment["projectId"] for assignment in response.json()["assignedProjects"]
    ] == list(graph_projects)

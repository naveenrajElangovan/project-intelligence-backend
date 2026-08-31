"""Asking a question reads the vector index. It does not call Atlassian.

Gating chat on a connected integration made a fully indexed project unusable as
soon as its OAuth connections were absent -- the state any rebuilt control plane
starts in -- and the client has no connect-integration screen to recover from.
"""

from app.api.integrations import _can_ask_questions
from app.projects.models import ProjectDefinition, VectorStoreRoute


def _project(*, active: bool = True, collection_name: str = "project-intelligence") -> ProjectDefinition:
    return ProjectDefinition(
        project_id="DEMO",
        display_name="Example Retail Platform",
        active=active,
        jira_projects=(),
        confluence_spaces=(),
        github_repositories=(),
        vector_store=VectorStoreRoute(collection_name=collection_name),
    )


def test_an_indexed_project_can_be_asked_without_any_connection():
    assert _can_ask_questions(_project()) is True


def test_an_inactive_project_cannot_be_asked():
    assert _can_ask_questions(_project(active=False)) is False


def test_a_project_without_a_collection_cannot_be_asked():
    assert _can_ask_questions(_project(collection_name="")) is False

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api import chat as chat_api
from app.api.chat_contracts import chat_response
from app.auth.dependencies import get_current_principal
from app.auth.models import EntraPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.models import ProjectAccessContext
from app.config import Settings, get_settings
from app.conversations.dependencies import get_conversation_store
from app.conversations.store import (
    ConversationHistoryRecord,
    ConversationMessageRecord,
    ConversationSummaryRecord,
    PendingTurn,
)
from app.main import app
from app.projects.dependencies import get_project_store
from app.projects.models import VectorStoreRoute


class AccessReader:
    def __init__(self, projects: tuple[str, ...]) -> None:
        self.projects = projects

    async def read(self, _: str) -> ProjectAccessContext:
        return ProjectAccessContext(
            projects=self.projects,
            project_roles={project: "DEVELOPER" for project in self.projects},
        )


def test_jira_result_page_is_preserved_for_public_clients() -> None:
    response = chat_response(
        "DEMO",
        "00000000-0000-0000-0000-000000000001",
        {
            "answer": "Jira has 60 tickets. Showing 1-50 of 60.",
            "confidence": "HIGH",
            "sources": [],
            "missingInformation": [],
            "resultPage": {
                "start": 1,
                "end": 50,
                "returned": 50,
                "total": 60,
                "hasMore": True,
            },
        },
    )

    assert response.result_page is not None
    assert response.result_page.total == 60
    assert response.result_page.has_more is True


class ProjectStore:
    async def get(self, project_id: str):
        if project_id != "DEMO":
            return None

        class Project:
            vector_store = VectorStoreRoute("project-intelligence", "chunk_text")
            jira_projects = (object(),)
            confluence_spaces = (object(),)
            github_repositories = (object(),)

        return Project()


class ConversationStore:
    async def create_conversation(self, **kwargs):
        assert kwargs == {"owner_id": "user-id", "project_id": "DEMO"}
        now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
        return ConversationSummaryRecord(
            conversation_id="00000000-0000-0000-0000-000000000002",
            title="New chat",
            created_at=now,
            updated_at=now,
        )

    async def begin_turn(self, **_kwargs) -> PendingTurn:
        return PendingTurn("00000000-0000-0000-0000-000000000001", "turn-1", ())

    async def complete_turn(self, *_args, **_kwargs) -> None:
        return None

    async def abandon_turn(self, *_args, **_kwargs) -> None:
        return None

    async def list_conversations(self, **kwargs):
        assert kwargs == {
            "owner_id": "user-id",
            "project_id": "DEMO",
            "limit": 50,
            "offset": 0,
        }
        return (
            ConversationSummaryRecord(
                conversation_id="00000000-0000-0000-0000-000000000001",
                title="What is the status?",
                created_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
                updated_at=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
            ),
        )

    async def get_conversation(self, **kwargs):
        assert kwargs == {
            "owner_id": "user-id",
            "project_id": "DEMO",
            "conversation_id": "00000000-0000-0000-0000-000000000001",
        }
        summary = (
            await self.list_conversations(owner_id="user-id", project_id="DEMO", limit=50, offset=0)
        )[0]
        return ConversationHistoryRecord(
            conversation=summary,
            messages=(
                ConversationMessageRecord(
                    role="user",
                    content="What is the status?",
                    created_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
                ),
                ConversationMessageRecord(
                    role="assistant",
                    content="The project is on track.",
                    created_at=datetime(2026, 8, 18, 12, 0, 1, tzinfo=UTC),
                ),
            ),
        )


def principal() -> EntraPrincipal:
    return EntraPrincipal(
        object_id="user-id",
        subject="subject",
        tenant_id="tenant",
        display_name="Developer",
        username="developer@example.com",
        email="developer@example.com",
        claims={},
    )


def configure(projects: tuple[str, ...]) -> None:
    app.dependency_overrides[get_current_principal] = principal
    app.dependency_overrides[get_graph_project_access_reader] = lambda: AccessReader(projects)
    app.dependency_overrides[get_project_store] = ProjectStore
    app.dependency_overrides[get_conversation_store] = ConversationStore
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        rag_service_url="http://rag:8002",
        rag_internal_api_key="r" * 32,
    )


def test_chat_rejects_project_before_rag_call(monkeypatch) -> None:
    configure(())
    called = False

    async def answer(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(chat_api.RagServiceClient, "answer", answer)
    try:
        response = TestClient(app).post(
            "/v1/projects/DEMO/chat",
            headers={"Authorization": "Bearer test"},
            json={"question": "What is the status?"},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 403
    assert called is False


def test_conversations_restore_for_stable_entra_owner_after_new_session() -> None:
    configure(("DEMO",))
    try:
        first_session = TestClient(app).get(
            "/v1/projects/DEMO/conversations",
            headers={"Authorization": "Bearer first-access-token"},
        )
        second_session = TestClient(app).get(
            "/v1/projects/DEMO/conversations",
            headers={"Authorization": "Bearer refreshed-access-token"},
        )
    finally:
        app.dependency_overrides.clear()

    assert first_session.status_code == 200
    assert second_session.status_code == 200
    assert first_session.json() == second_session.json()
    assert first_session.json()[0]["conversationId"] == ("00000000-0000-0000-0000-000000000001")


def test_new_chat_creates_a_server_owned_conversation_id() -> None:
    configure(("DEMO",))
    try:
        response = TestClient(app).post(
            "/v1/projects/DEMO/conversations",
            headers={"Authorization": "Bearer refreshed-access-token"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["conversationId"] == "00000000-0000-0000-0000-000000000002"
    assert response.json()["title"] == "New chat"


def test_conversation_list_accepts_explicit_pagination() -> None:
    configure(("DEMO",))
    try:
        response = TestClient(app).get(
            "/v1/projects/DEMO/conversations?limit=50&offset=0",
            headers={"Authorization": "Bearer refreshed-access-token"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def test_conversation_detail_restores_completed_messages() -> None:
    configure(("DEMO",))
    try:
        response = TestClient(app).get(
            "/v1/projects/DEMO/conversations/00000000-0000-0000-0000-000000000001",
            headers={"Authorization": "Bearer refreshed-access-token"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["title"] == "What is the status?"
    assert response.json()["messages"] == [
        {
            "role": "user",
            "content": "What is the status?",
            "createdAt": "2026-08-18T12:00:00Z",
        },
        {
            "role": "assistant",
            "content": "The project is on track.",
            "createdAt": "2026-08-18T12:00:01Z",
        },
    ]


def test_conversation_history_is_denied_before_mongo_lookup() -> None:
    configure(())
    try:
        response = TestClient(app).get(
            "/v1/projects/DEMO/conversations",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_chat_passes_only_backend_created_policies(monkeypatch) -> None:
    configure(("DEMO",))

    async def answer(
        self,
        project_id,
        question,
        policies,
        collection_name,
        text_field,
        embedding_field,
        embedding_model,
        schema_version,
        model_profile,
        request_id,
        conversation_history,
        conversation_context,
        enabled_providers=None,
    ):
        assert project_id == "DEMO"
        assert policies == ("project:DEMO", "user:user-id", "role:DEMO:DEVELOPER")
        assert collection_name == "project-intelligence"
        assert text_field == "chunk_text"
        assert embedding_field == "embedding_text"
        assert embedding_model == "multilingual-e5-large"
        assert schema_version == "3"
        assert model_profile == "budget"
        assert request_id
        assert conversation_history == ()
        assert conversation_context == {
            "version": 3,
            "summary": "",
            "activeSubject": "",
            "entities": [],
            "lastIntent": "",
            "lastResolvedQuestion": "",
            "stateRevision": 0,
            "structuredScope": None,
        }
        assert enabled_providers is None
        return {
            "answer": "Grounded answer.",
            "confidence": "HIGH",
            "sources": [{"type": "CODE", "title": "main.py", "reference": "repo:main.py:1-10"}],
            "missingInformation": [],
            "evidenceStatus": "SUFFICIENT",
            "contextQuality": "SUFFICIENT",
            "contextRelevance": 0.93,
            "contextCompleteness": 0.88,
            "degradation": ["LEXICAL_RETRIEVAL_FALLBACK"],
        }

    monkeypatch.setattr(chat_api.RagServiceClient, "answer", answer)
    try:
        response = TestClient(app).post(
            "/v1/projects/DEMO/chat",
            headers={"Authorization": "Bearer test"},
            json={"question": "What is the status?", "accessPolicyIds": ["project:AAOS"]},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["answer"] == "Grounded answer."
    assert response.json()["sources"][0]["reference"] == "repo:main.py:1-10"
    assert response.json()["evidenceStatus"] == "SUFFICIENT"
    assert response.json()["contextQuality"] == "SUFFICIENT"
    assert response.json()["contextRelevance"] == 0.93
    assert response.json()["contextCompleteness"] == 0.88
    assert response.json()["degradation"] == ["LEXICAL_RETRIEVAL_FALLBACK"]
    assert response.json()["conversationId"] == "00000000-0000-0000-0000-000000000001"
    assert response.headers["x-request-id"]


def test_chat_missing_answer_is_not_reported_as_authorization_denial(monkeypatch) -> None:
    configure(("DEMO",))

    async def answer(*args, **kwargs):
        return {"confidence": "NONE", "sources": [], "missingInformation": []}

    monkeypatch.setattr(chat_api.RagServiceClient, "answer", answer)
    try:
        response = TestClient(app).post(
            "/v1/projects/DEMO/chat",
            headers={"Authorization": "Bearer test"},
            json={"question": "What is not documented?"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "enough evidence" in response.json()["answer"]
    assert "authorized" not in response.json()["answer"].casefold()


def test_context_update_is_accepted_only_for_verified_answered_responses() -> None:
    update = {"standaloneQuestion": "How does Atlas work?"}

    assert (
        chat_api._successful_context_update(
            {"status": "ANSWERED", "confidence": "HIGH", "conversationContextUpdate": update}
        )
        == update
    )
    assert (
        chat_api._successful_context_update(
            {
                "status": "INSUFFICIENT_EVIDENCE",
                "confidence": "NONE",
                "conversationContextUpdate": update,
            }
        )
        is None
    )
    assert (
        chat_api._successful_context_update(
            {
                "status": "NEEDS_CLARIFICATION",
                "confidence": "NONE",
                "conversationContextUpdate": update,
            }
        )
        is None
    )


def test_chat_stream_preserves_server_created_policies_and_verified_events(monkeypatch) -> None:
    configure(("DEMO",))

    async def stream_answer(self, project_id, question, policies, *args, **kwargs):
        assert project_id == "DEMO"
        assert question == "What does POS do?"
        assert policies == ("project:DEMO", "user:user-id", "role:DEMO:DEVELOPER")
        yield {"type": "status", "stage": "retrieving", "message": "Searching…"}
        yield {
            "type": "answer_delta",
            "delta": "Grounded answer.",
            "verification": "pending",
        }
        yield {"type": "answer_snapshot", "answer": "Buffered fallback."}
        yield {
            "type": "answer_sentence_verified",
            "claimIndex": 1,
            "sentence": "Grounded answer.",
            "provisionalText": "Grounded answer [SOURCE 1].",
        }
        yield {
            "type": "answer_sentence_rejected",
            "claimIndex": 2,
            "sentence": "Unsupported answer.",
            "reason": "UNSUPPORTED_CLAIM",
        }
        yield {
            "type": "complete",
            "response": {
                "answer": "Grounded answer.",
                "confidence": "HIGH",
                "projectId": "DEMO",
                "sources": [],
                "missingInformation": [],
            },
        }

    monkeypatch.setattr(chat_api.RagServiceClient, "stream_answer", stream_answer)
    try:
        response = TestClient(app).post(
            "/v1/projects/DEMO/chat/stream",
            headers={"Authorization": "Bearer test"},
            json={"question": "What does POS do?"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == [
        "status",
        "answer_delta",
        "answer_snapshot",
        "answer_sentence_verified",
        "answer_sentence_rejected",
        "complete",
    ]
    assert events[1]["verification"] == "pending"
    assert events[2]["answer"] == "Buffered fallback."
    assert events[3]["claimIndex"] == 1
    assert events[3]["provisionalText"] == "Grounded answer [SOURCE 1]."
    assert events[4]["claimIndex"] == 2
    assert events[-1]["response"]["answer"] == "Grounded answer."


def test_selected_providers_are_validated_and_forwarded(monkeypatch) -> None:
    configure(("DEMO",))
    observed = None

    async def answer(*args, **kwargs):
        nonlocal observed
        observed = kwargs.get("enabled_providers")
        return {
            "answer": "Jira answer.",
            "confidence": "HIGH",
            "sources": [],
            "missingInformation": [],
        }

    monkeypatch.setattr(chat_api.RagServiceClient, "answer", answer)
    try:
        accepted = TestClient(app).post(
            "/v1/projects/DEMO/chat",
            headers={"Authorization": "Bearer test"},
            json={"question": "How many Jira tickets?", "enabledProviders": ["JIRA"]},
        )
    finally:
        app.dependency_overrides.clear()

    assert accepted.status_code == 200
    assert observed == ("JIRA",)


def test_unconfigured_provider_selection_is_rejected() -> None:
    project = SimpleNamespace(
        jira_projects=(object(),),
        confluence_spaces=(),
        github_repositories=(),
    )

    try:
        chat_api._enabled_providers(project, ["CONFLUENCE"])
    except Exception as error:
        assert getattr(error, "status_code", None) == 422
    else:
        raise AssertionError("unconfigured provider selection was accepted")

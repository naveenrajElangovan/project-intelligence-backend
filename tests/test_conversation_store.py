import asyncio
from datetime import UTC, datetime, timedelta

from app.conversations.store import (
    ConversationContextRecord,
    MongoConversationStore,
    PendingTurn,
)


class ConversationsCollection:
    def __init__(self) -> None:
        self.inserted: dict | None = None
        self.inserted_documents: list[dict] = []
        self.find_one_called = False

    async def find_one(self, *_args, **_kwargs):
        self.find_one_called = True
        return {"_id": "existing-conversation"}

    async def insert_one(self, document: dict) -> None:
        self.inserted = document
        self.inserted_documents.append(document)


class ConversationCursor:
    def __init__(self) -> None:
        self.skipped: int | None = None
        self.limited: int | None = None

    def sort(self, *_args):
        return self

    def skip(self, value: int):
        self.skipped = value
        return self

    def limit(self, value: int):
        self.limited = value
        return self

    async def to_list(self, *, length: int):
        assert length == self.limited
        return []


class PagedConversationsCollection:
    def __init__(self) -> None:
        self.cursor = ConversationCursor()

    def find(self, *_args, **_kwargs):
        return self.cursor


class UpdatingConversationsCollection:
    def __init__(self) -> None:
        self.query: dict | None = None
        self.update: dict | None = None

    async def update_one(self, query: dict, update: dict):
        self.query = query
        self.update = update


def test_semantic_context_update_is_bounded_and_revision_guarded() -> None:
    store = MongoConversationStore.__new__(MongoConversationStore)
    conversations = UpdatingConversationsCollection()
    store._conversations = conversations
    pending = PendingTurn(
        "conversation-1",
        "turn-1",
        (),
        ConversationContextRecord(state_revision=4),
    )

    asyncio.run(
        store._update_context(
            pending,
            owner_id="owner-1",
            project_id="DEMO",
            update={
                "standaloneQuestion": "How does POS close shift work?",
                "activeSubject": "POS close shift",
                "intent": "CODE_ASSISTED",
                "resolutionConfidence": 0.95,
                "entities": [
                    {
                        "type": "feature",
                        "value": "POS close shift",
                        "canonicalValue": "POS close shift",
                    }
                ],
            },
        )
    )

    assert conversations.query == {
        "_id": "conversation-1",
        "owner_id": "owner-1",
        "project_id": "DEMO",
        "context.state_revision": 4,
    }
    assert conversations.update is not None
    assert conversations.update["$set"]["context.active_subject"] == "POS close shift"
    assert conversations.update["$set"]["context.last_resolved_question"] == (
        "How does POS close shift work?"
    )
    assert conversations.update["$inc"] == {"context.state_revision": 1}


def test_low_confidence_context_update_is_not_persisted() -> None:
    store = MongoConversationStore.__new__(MongoConversationStore)
    conversations = UpdatingConversationsCollection()
    store._conversations = conversations
    pending = PendingTurn("conversation-1", "turn-1", ())

    asyncio.run(
        store._update_context(
            pending,
            owner_id="owner-1",
            project_id="DEMO",
            update={
                "standaloneQuestion": "What about that?",
                "resolutionConfidence": 0.4,
            },
        )
    )

    assert conversations.query is None
    assert conversations.update is None


def test_missing_conversation_id_creates_a_distinct_new_chat() -> None:
    store = MongoConversationStore.__new__(MongoConversationStore)
    conversations = ConversationsCollection()
    store._conversations = conversations
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    store._retention = timedelta(days=30)

    conversation_id = asyncio.run(
        store._resolve_conversation(
            owner_id="stable-entra-object-id",
            project_id="DEMO",
            requested_id=None,
            now=now,
            first_question="Start a genuinely new chat",
        )
    )

    assert conversation_id != "existing-conversation"
    assert conversations.find_one_called is False
    assert conversations.inserted == {
        "_id": conversation_id,
        "owner_id": "stable-entra-object-id",
        "project_id": "DEMO",
        "title": "Start a genuinely new chat",
        "created_at": now,
        "updated_at": now,
        "expires_at": now + store._retention,
        "context": {
            "version": 1,
            "summary": "",
            "active_subject": "",
            "entities": [],
            "last_intent": "",
            "last_resolved_question": "",
            "state_revision": 0,
        },
    }


def test_conversation_listing_applies_requested_page_offset_and_limit() -> None:
    store = MongoConversationStore.__new__(MongoConversationStore)
    conversations = PagedConversationsCollection()
    store._conversations = conversations

    result = asyncio.run(
        store.list_conversations(
            owner_id="stable-entra-object-id",
            project_id="DEMO",
            offset=75,
            limit=25,
        )
    )

    assert result == ()
    assert conversations.cursor.skipped == 75
    assert conversations.cursor.limited == 25


def test_explicit_new_chats_receive_distinct_server_conversation_ids() -> None:
    store = MongoConversationStore.__new__(MongoConversationStore)
    conversations = ConversationsCollection()
    store._conversations = conversations
    store._retention = timedelta(days=30)

    first = asyncio.run(
        store.create_conversation(owner_id="stable-entra-object-id", project_id="DEMO")
    )
    second = asyncio.run(
        store.create_conversation(owner_id="stable-entra-object-id", project_id="DEMO")
    )

    assert first.conversation_id != second.conversation_id
    assert {document["_id"] for document in conversations.inserted_documents} == {
        first.conversation_id,
        second.conversation_id,
    }

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from app.conversations.models import (
    ConversationContextRecord,
    ConversationHistoryRecord,
    ConversationMessageRecord,
    ConversationNotFoundError,
    ConversationSummaryRecord,
    HistoryMessage,
    PendingTurn,
)


# A pasted JSON document may be up to a megabyte, and it is a conversion input
# rather than a conversational turn. _load_history already clamps what is
# replayed to the RAG, so the contract is safe either way; what this prevents is
# the collection growing by a megabyte per paste for text nothing will ever read
# past the first few thousand characters. The marker keeps the truncation honest
# rather than silent.
MAX_STORED_TURN_CHARACTERS = 4_000


def _stored_text(value: str) -> str:
    if len(value) <= MAX_STORED_TURN_CHARACTERS:
        return value
    omitted = len(value) - MAX_STORED_TURN_CHARACTERS
    return f"{value[:MAX_STORED_TURN_CHARACTERS]}\n[truncated: {omitted} more characters]"


def _safe_structured_scope(value: object) -> dict[str, object] | None:
    """Validate provider query memory before it enters the server-owned context."""

    if not isinstance(value, dict) or value.get("provider") != "JIRA":
        return None
    allowed_filters = {
        "labels",
        "status",
        "status_category",
        "status_category_key",
        "resolution",
        "resolution_id",
        "issue_type",
        "priority",
        "fixed",
        "issue_key",
        "section_kind",
        "topic",
    }
    raw_filters = value.get("filters", {})
    if not isinstance(raw_filters, dict) or len(raw_filters) > 8:
        return None
    filters: dict[str, list[str]] = {}
    for key, raw_values in raw_filters.items():
        if key not in allowed_filters or not isinstance(raw_values, (list, tuple)):
            return None
        values = [str(item).strip()[:200] for item in raw_values[:20] if str(item).strip()]
        if not values:
            return None
        filters[str(key)] = values
    operation = str(value.get("operation") or "")
    if operation not in {
        "OVERVIEW",
        "COUNT",
        "LIST",
        "DISTRIBUTION",
        "DETAIL",
        "SECTION",
        "SECTION_COUNT",
    }:
        return None
    try:
        page_size = min(500, max(1, int(value.get("pageSize") or 50)))
        next_offset = max(0, int(value.get("nextOffset") or 0))
    except (TypeError, ValueError):
        return None
    group_by = str(value.get("groupBy") or "")[:40] or None
    if group_by not in {None, "status", "issue_type", "priority"}:
        return None
    return {
        "provider": "JIRA",
        "resourceType": "ISSUE",
        "filters": filters,
        "operation": operation,
        "groupBy": group_by,
        "snapshotAt": str(value.get("snapshotAt") or "")[:100] or None,
        "complete": bool(value.get("complete")),
        "pageSize": page_size,
        "nextOffset": next_offset,
        "activeSubject": str(value.get("activeSubject") or "")[:500],
    }


class MongoConversationStore:
    """Store project-scoped conversations with MongoDB TTL retention."""

    def __init__(
        self,
        mongodb_url: str,
        database_name: str,
        *,
        retention_days: int,
        history_turns: int,
    ) -> None:
        self._client = AsyncMongoClient(
            mongodb_url,
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            appname="project-intelligence-backend",
        )
        database = self._client[database_name]
        self._conversations = database["conversations"]
        self._turns = database["turns"]
        self._retention = timedelta(days=retention_days)
        self._history_turns = history_turns

    async def initialize(self) -> None:
        """Verify MongoDB and create ownership, ordering, and TTL indexes."""

        await self._client.admin.command("ping")
        await self._conversations.create_index(
            [("owner_id", ASCENDING), ("project_id", ASCENDING), ("updated_at", DESCENDING)],
            name="conversation_owner_project_updated",
        )
        await self._conversations.create_index(
            "expires_at", expireAfterSeconds=0, name="conversation_retention_ttl"
        )
        await self._turns.create_index(
            [("conversation_id", ASCENDING), ("created_at", DESCENDING)],
            name="turn_conversation_created",
        )
        await self._turns.create_index(
            [
                ("owner_id", ASCENDING),
                ("project_id", ASCENDING),
                ("conversation_id", ASCENDING),
                ("created_at", ASCENDING),
            ],
            name="turn_owner_project_conversation_created",
        )
        await self._turns.create_index(
            "expires_at", expireAfterSeconds=0, name="turn_retention_ttl"
        )

    async def close(self) -> None:
        """Release MongoDB sockets during application shutdown."""

        await self._client.close()

    async def list_conversations(
        self, *, owner_id: str, project_id: str, limit: int, offset: int = 0
    ) -> tuple[ConversationSummaryRecord, ...]:
        """List one page of a user's conversations without exposing other owners."""

        cursor = (
            self._conversations.find(
                {"owner_id": owner_id, "project_id": project_id},
                {"title": 1, "created_at": 1, "updated_at": 1},
            )
            .sort("updated_at", DESCENDING)
            .skip(offset)
            .limit(limit)
        )
        documents = await cursor.to_list(length=limit)
        return tuple(self._conversation_summary(document) for document in documents)

    async def get_conversation(
        self, *, owner_id: str, project_id: str, conversation_id: str
    ) -> ConversationHistoryRecord:
        """Load all unexpired completed messages for one strictly owned conversation."""

        conversation = await self._conversations.find_one(
            {
                "_id": conversation_id,
                "owner_id": owner_id,
                "project_id": project_id,
            },
            {"title": 1, "created_at": 1, "updated_at": 1},
        )
        if conversation is None:
            raise ConversationNotFoundError("The conversation is unavailable.")

        cursor = self._turns.find(
            {
                "conversation_id": conversation_id,
                "owner_id": owner_id,
                "project_id": project_id,
                "status": "COMPLETE",
            },
            {"question": 1, "answer": 1, "created_at": 1, "completed_at": 1},
        ).sort("created_at", ASCENDING)
        turns = await cursor.to_list(length=None)
        messages: list[ConversationMessageRecord] = []
        for turn in turns:
            created_at = self._utc_datetime(turn.get("created_at"))
            question = str(turn.get("question") or "")
            answer = str(turn.get("answer") or "")
            if question:
                messages.append(ConversationMessageRecord("user", question, created_at))
            if answer:
                messages.append(
                    ConversationMessageRecord(
                        "assistant",
                        answer,
                        self._utc_datetime(turn.get("completed_at"), fallback=created_at),
                    )
                )
        return ConversationHistoryRecord(
            conversation=self._conversation_summary(conversation),
            messages=tuple(messages),
        )

    async def create_conversation(
        self, *, owner_id: str, project_id: str
    ) -> ConversationSummaryRecord:
        """Create one empty owner-scoped chat with a unique server identifier."""

        now = datetime.now(UTC)
        document = self._new_conversation_document(
            owner_id=owner_id,
            project_id=project_id,
            title="New chat",
            now=now,
        )
        await self._conversations.insert_one(document)
        return self._conversation_summary(document)

    async def begin_turn(
        self,
        *,
        owner_id: str,
        project_id: str,
        question: str,
        conversation_id: str | None,
    ) -> PendingTurn:
        """Resolve an owned conversation, load completed history, and persist the user turn."""

        now = datetime.now(UTC)
        resolved_id = await self._resolve_conversation(
            owner_id=owner_id,
            project_id=project_id,
            requested_id=conversation_id,
            now=now,
            first_question=question,
        )
        history = await self._load_history(owner_id, project_id, resolved_id)
        context = await self._load_context(owner_id, project_id, resolved_id)
        turn_id = str(uuid4())
        await self._turns.insert_one(
            {
                "_id": turn_id,
                "conversation_id": resolved_id,
                "owner_id": owner_id,
                "project_id": project_id,
                "question": _stored_text(question),
                "answer": None,
                "status": "PENDING",
                "created_at": now,
                "expires_at": now + self._retention,
            }
        )
        await self._touch_conversation(resolved_id, owner_id, project_id, now)
        return PendingTurn(resolved_id, turn_id, history, context)

    async def complete_turn(
        self,
        pending: PendingTurn,
        *,
        owner_id: str,
        project_id: str,
        answer: str,
        confidence: str,
        context_update: dict[str, object] | None = None,
    ) -> None:
        """Atomically mark one owned pending turn as completed with its verified answer."""

        result = await self._turns.update_one(
            {
                "_id": pending.turn_id,
                "conversation_id": pending.conversation_id,
                "owner_id": owner_id,
                "project_id": project_id,
                "status": "PENDING",
            },
            {
                "$set": {
                    "answer": answer,
                    "confidence": confidence,
                    "status": "COMPLETE",
                    "completed_at": datetime.now(UTC),
                }
            },
        )
        if result.modified_count != 1:
            raise ConversationNotFoundError("The pending conversation turn is unavailable.")
        await self._update_context(
            pending,
            owner_id=owner_id,
            project_id=project_id,
            update=context_update,
        )

    async def abandon_turn(self, pending: PendingTurn, *, owner_id: str, project_id: str) -> None:
        """Remove an unanswered pending turn so it cannot pollute later context."""

        await self._turns.delete_one(
            {
                "_id": pending.turn_id,
                "conversation_id": pending.conversation_id,
                "owner_id": owner_id,
                "project_id": project_id,
                "status": "PENDING",
            }
        )

    async def _resolve_conversation(
        self,
        *,
        owner_id: str,
        project_id: str,
        requested_id: str | None,
        now: datetime,
        first_question: str,
    ) -> str:
        """Validate an explicit conversation or create a separate new conversation."""

        ownership = {"owner_id": owner_id, "project_id": project_id}
        if requested_id:
            conversation = await self._conversations.find_one(
                {"_id": requested_id, **ownership}, {"_id": 1}
            )
            if conversation is None:
                raise ConversationNotFoundError("The conversation is unavailable.")
            await self._conversations.update_one(
                {"_id": requested_id, **ownership, "title": "New chat"},
                {"$set": {"title": first_question.strip()[:100]}},
            )
            return requested_id
        document = self._new_conversation_document(
            owner_id=owner_id,
            project_id=project_id,
            title=first_question.strip()[:100],
            now=now,
        )
        await self._conversations.insert_one(document)
        return str(document["_id"])

    def _new_conversation_document(
        self,
        *,
        owner_id: str,
        project_id: str,
        title: str,
        now: datetime,
    ) -> dict:
        return {
            "_id": str(uuid4()),
            "owner_id": owner_id,
            "project_id": project_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + self._retention,
            "context": {
                "version": 3,
                "summary": "",
                "active_subject": "",
                "entities": [],
                "last_intent": "",
                "last_resolved_question": "",
                "state_revision": 0,
                "structured_scope": None,
            },
        }

    async def _load_context(
        self, owner_id: str, project_id: str, conversation_id: str
    ) -> ConversationContextRecord:
        """Load semantic memory independently from the bounded transcript window."""

        document = await self._conversations.find_one(
            {
                "_id": conversation_id,
                "owner_id": owner_id,
                "project_id": project_id,
            },
            {"context": 1},
        )
        raw = document.get("context", {}) if isinstance(document, dict) else {}
        entities = raw.get("entities", []) if isinstance(raw, dict) else []
        safe_entities = tuple(
            {
                "type": str(entity.get("type") or "subject")[:40],
                "value": str(entity.get("value") or "")[:500],
                "canonicalValue": str(
                    entity.get("canonicalValue")
                    or entity.get("canonical_value")
                    or entity.get("value")
                    or ""
                )[:500],
            }
            for entity in entities[:12]
            if isinstance(entity, dict) and str(entity.get("value") or "").strip()
        )
        try:
            version = int(raw.get("version") or 1) if isinstance(raw, dict) else 1
            revision = int(raw.get("state_revision") or 0) if isinstance(raw, dict) else 0
        except (TypeError, ValueError):
            version, revision = 1, 0
        legacy = version < 2
        structured_scope = (
            _safe_structured_scope(raw.get("structured_scope"))
            if isinstance(raw, dict) and version >= 3
            else None
        )
        return ConversationContextRecord(
            version=3,
            summary="" if legacy else str(raw.get("summary") or "")[:2000],
            active_subject="" if legacy else str(raw.get("active_subject") or "")[:500],
            entities=() if legacy else safe_entities,
            last_intent="" if legacy else str(raw.get("last_intent") or "")[:80],
            last_resolved_question=""
            if legacy
            else str(raw.get("last_resolved_question") or "")[:4000],
            state_revision=max(0, revision),
            structured_scope=structured_scope,
        )

    async def _update_context(
        self,
        pending: PendingTurn,
        *,
        owner_id: str,
        project_id: str,
        update: dict[str, object] | None,
    ) -> None:
        """Commit a validated semantic update without overwriting a newer turn."""

        if not isinstance(update, dict):
            return
        subject = str(update.get("activeSubject") or "").strip()[:500]
        standalone = str(update.get("standaloneQuestion") or "").strip()[:4000]
        intent = str(update.get("intent") or "").strip()[:80]
        confidence = update.get("resolutionConfidence", 0.0)
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        if confidence_value < 0.6 or not standalone:
            return
        entities = update.get("entities", [])
        safe_entities = (
            [
                {
                    "type": str(entity.get("type") or "subject")[:40],
                    "value": str(entity.get("value") or "")[:500],
                    "canonicalValue": str(
                        entity.get("canonicalValue") or entity.get("value") or ""
                    )[:500],
                }
                for entity in entities[:12]
                if isinstance(entity, dict) and str(entity.get("value") or "").strip()
            ]
            if isinstance(entities, list)
            else []
        )
        summary = f"Active subject: {subject}" if subject else ""
        structured_scope = _safe_structured_scope(update.get("structuredScope"))
        revision_filter: dict[str, object] = {
            "context.state_revision": pending.context.state_revision
        }
        if pending.context.state_revision == 0:
            revision_filter = {
                "$or": [
                    {"context.state_revision": 0},
                    {"context.state_revision": {"$exists": False}},
                ]
            }
        await self._conversations.update_one(
            {
                "_id": pending.conversation_id,
                "owner_id": owner_id,
                "project_id": project_id,
                **revision_filter,
            },
            {
                "$set": {
                    "context.version": 3,
                    "context.summary": summary[:2000],
                    "context.active_subject": subject,
                    "context.entities": safe_entities,
                    "context.last_intent": intent,
                    "context.last_resolved_question": standalone,
                    "context.structured_scope": structured_scope,
                },
                "$inc": {"context.state_revision": 1},
            },
        )

    async def _load_history(
        self, owner_id: str, project_id: str, conversation_id: str
    ) -> tuple[HistoryMessage, ...]:
        """Load recent completed turns in chronological order with strict scope filters."""

        cursor = (
            self._turns.find(
                {
                    "conversation_id": conversation_id,
                    "owner_id": owner_id,
                    "project_id": project_id,
                    "status": "COMPLETE",
                    "confidence": {"$in": ["MEDIUM", "HIGH"]},
                },
                {"question": 1, "answer": 1, "confidence": 1, "created_at": 1},
            )
            .sort("created_at", DESCENDING)
            .limit(self._history_turns)
        )
        turns = list(reversed(await cursor.to_list(length=self._history_turns)))
        messages: list[HistoryMessage] = []
        for turn in turns:
            messages.append(HistoryMessage("user", str(turn.get("question") or "")[:4000]))
            messages.append(HistoryMessage("assistant", str(turn.get("answer") or "")[:8000]))
        return tuple(message for message in messages if message.content)

    async def _touch_conversation(
        self, conversation_id: str, owner_id: str, project_id: str, now: datetime
    ) -> None:
        """Extend only conversation metadata; individual turns retain their own 30-day expiry."""

        result = await self._conversations.update_one(
            {"_id": conversation_id, "owner_id": owner_id, "project_id": project_id},
            {"$set": {"updated_at": now, "expires_at": now + self._retention}},
        )
        if result.matched_count != 1:
            raise ConversationNotFoundError("The conversation is unavailable.")

    @classmethod
    def _conversation_summary(cls, document: dict) -> ConversationSummaryRecord:
        created_at = cls._utc_datetime(document.get("created_at"))
        return ConversationSummaryRecord(
            conversation_id=str(document["_id"]),
            title=str(document.get("title") or "New chat"),
            created_at=created_at,
            updated_at=cls._utc_datetime(document.get("updated_at"), fallback=created_at),
        )

    @staticmethod
    def _utc_datetime(value: object, *, fallback: datetime | None = None) -> datetime:
        if not isinstance(value, datetime):
            return fallback or datetime.now(UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

"""Domain records for project-scoped conversation memory.

These values are storage-agnostic so application and API layers do not depend
on MongoDB document details.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class HistoryMessage:
    """A completed message safe to pass as bounded conversation context."""

    role: str
    content: str


@dataclass(frozen=True)
class ConversationContextRecord:
    """Bounded semantic memory; it is never authoritative retrieval evidence."""

    version: int = 3
    summary: str = ""
    active_subject: str = ""
    entities: tuple[dict[str, str], ...] = ()
    last_intent: str = ""
    last_resolved_question: str = ""
    state_revision: int = 0
    structured_scope: dict[str, object] | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "summary": self.summary,
            "activeSubject": self.active_subject,
            "entities": list(self.entities),
            "lastIntent": self.last_intent,
            "lastResolvedQuestion": self.last_resolved_question,
            "stateRevision": self.state_revision,
            "structuredScope": self.structured_scope,
        }


@dataclass(frozen=True)
class PendingTurn:
    conversation_id: str
    turn_id: str
    history: tuple[HistoryMessage, ...]
    context: ConversationContextRecord = ConversationContextRecord()


@dataclass(frozen=True)
class ConversationSummaryRecord:
    conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConversationMessageRecord:
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class ConversationHistoryRecord:
    conversation: ConversationSummaryRecord
    messages: tuple[ConversationMessageRecord, ...]


class ConversationNotFoundError(ValueError):
    """Raised when a conversation is missing or belongs to another scope."""

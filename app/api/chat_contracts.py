"""Public HTTP contracts and mapping for project chat endpoints."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.conversations.models import ConversationHistoryRecord, ConversationSummaryRecord



# A prose question is interpolated into prompts, stored as conversation history,
# and rewritten by the planner, so it stays small. A pasted JSON document is
# none of those: the RAG converts it deterministically before its graph runs and
# never sends it to a model. Rejecting it here with a 422 would make that path
# unreachable, so the ceiling is conditional on shape rather than uniform.
MAX_PROSE_QUESTION_CHARACTERS = 4_000
MAX_PAYLOAD_QUESTION_CHARACTERS = 1_000_000


def _looks_like_payload(question: str) -> bool:
    """Cheap structural test, matching app/models.py in the RAG service.

    Intentionally not a parser. The RAG owns decoding, repair, and error
    reporting; this only decides whether a long body is admitted at all.
    """

    stripped = question.strip()
    if len(stripped) <= MAX_PROSE_QUESTION_CHARACTERS:
        return True
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = (
            stripped[4:].strip() if stripped[:4].lower() == "json" else stripped.strip()
        )
    opener, closer = stripped[:1], stripped[-1:]
    if (opener, closer) in {("{", "}"), ("[", "]")}:
        return True
    return opener == '"' and closer == '"' and ("{" in stripped or "[" in stripped)


class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=MAX_PAYLOAD_QUESTION_CHARACTERS)
    mode: Literal["ASK", "ANALYZE", "DEEP_ANALYSIS"] = "ASK"
    conversation_id: str | None = Field(
        default=None, alias="conversationId", min_length=36, max_length=36
    )

    @model_validator(mode="after")
    def bound_prose_questions(self) -> "ChatRequest":
        if not _looks_like_payload(self.question):
            raise ValueError(
                "question may exceed "
                f"{MAX_PROSE_QUESTION_CHARACTERS} characters only when it is a "
                "single JSON document"
            )
        return self


class SourceReference(BaseModel):
    type: str
    title: str
    reference: str
    url: str | None = None
    locator: str | None = None
    language: str | None = None


class ChatResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    answer: str
    confidence: str
    project_id: str = Field(alias="projectId")
    data_freshness: dict[str, str] = Field(default_factory=dict, alias="dataFreshness")
    sources: list[SourceReference]
    recommended_actions: list[dict[str, str]] = Field(
        default_factory=list, alias="recommendedActions"
    )
    missing_information: list[str] = Field(alias="missingInformation")
    evidence_status: str = Field(default="UNKNOWN", alias="evidenceStatus")
    context_quality: str = Field(default="UNKNOWN", alias="contextQuality")
    context_relevance: float = Field(
        default=0.0, alias="contextRelevance", ge=0.0, le=1.0
    )
    context_completeness: float = Field(
        default=0.0, alias="contextCompleteness", ge=0.0, le=1.0
    )
    degradation: list[str] = Field(default_factory=list)
    conversation_id: str = Field(alias="conversationId")


class ConversationSummaryResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    conversation_id: str = Field(alias="conversationId")
    title: str
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class ConversationMessageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    role: Literal["user", "assistant"]
    content: str
    created_at: datetime = Field(alias="createdAt")


class ConversationHistoryResponse(ConversationSummaryResponse):
    messages: list[ConversationMessageResponse]


def conversation_summary_response(
    conversation: ConversationSummaryRecord,
) -> ConversationSummaryResponse:
    return ConversationSummaryResponse(
        conversation_id=conversation.conversation_id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def conversation_history_response(
    history: ConversationHistoryRecord,
) -> ConversationHistoryResponse:
    return ConversationHistoryResponse(
        **conversation_summary_response(history.conversation).model_dump(),
        messages=[
            ConversationMessageResponse(
                role=message.role,
                content=message.content,
                created_at=message.created_at,
            )
            for message in history.messages
        ],
    )


def chat_response(
    project_id: str, conversation_id: str, response: dict[str, object]
) -> ChatResponse:
    """Validate and normalize the private RAG payload for public clients."""

    sources = response.get("sources", [])
    missing_information = response.get("missingInformation", [])
    return ChatResponse(
        answer=str(
            response.get("answer")
            or "I could not find enough evidence in this project's indexed sources to answer this question."
        ),
        confidence=str(response.get("confidence") or "NONE"),
        project_id=project_id,
        conversation_id=conversation_id,
        sources=[
            SourceReference(
                type=str(source.get("type") or "DOCUMENT"),
                title=str(source.get("title") or "Untitled source"),
                reference=str(source.get("reference") or ""),
                url=str(source.get("url") or "") or None,
                locator=str(source.get("locator") or "") or None,
                language=str(source.get("language") or "") or None,
            )
            for source in sources
            if isinstance(source, dict)
        ]
        if isinstance(sources, list)
        else [],
        missing_information=[str(value) for value in missing_information]
        if isinstance(missing_information, list)
        else [],
        evidence_status=str(response.get("evidenceStatus") or "UNKNOWN"),
        context_quality=str(response.get("contextQuality") or "UNKNOWN"),
        context_relevance=float(response.get("contextRelevance") or 0.0),
        context_completeness=float(response.get("contextCompleteness") or 0.0),
        degradation=[str(value) for value in response.get("degradation", [])]
        if isinstance(response.get("degradation", []), list)
        else [],
    )

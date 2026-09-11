import json
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pymongo.errors import PyMongoError
from starlette.responses import StreamingResponse

from app.application.rag_client import RagServiceClient
from app.application.chat_policy import model_profile as _model_profile
from app.application.chat_policy import retrieval_policies as _retrieval_policies
from app.api.chat_contracts import (
    ChatRequest,
    ChatResponse,
    ConversationHistoryResponse,
    ConversationSummaryResponse,
    chat_response as _chat_response,
    conversation_history_response as _conversation_history_response,
    conversation_summary_response as _conversation_summary_response,
)
from app.auth.dependencies import CurrentPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.graph import GraphProjectAccessReader
from app.config import Settings, get_settings
from app.conversations.dependencies import get_conversation_store
from app.conversations.models import ConversationNotFoundError, PendingTurn
from app.conversations.store import MongoConversationStore
from app.projects.dependencies import get_project_store
from app.projects.store import ProjectStore
from app.telemetry import chat_event, pseudonymous_user, request_id, started

router = APIRouter(prefix="/v1/projects", tags=["chat"])


def _enabled_providers(project, requested: list[str] | None) -> tuple[str, ...] | None:
    """Validate client source selection against server-owned project mappings."""

    if requested is None:
        return None
    available = {
        *({"JIRA"} if project.jira_projects else set()),
        *({"CONFLUENCE"} if project.confluence_spaces else set()),
        *({"GITHUB"} if project.github_repositories else set()),
    }
    unavailable = sorted(set(requested).difference(available))
    if unavailable:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The selected provider is not available for this project.",
        )
    return tuple(requested)


def _rag_service_client(request: Request, settings: Settings) -> RagServiceClient:
    """Reuse the app-owned HTTP pool while remaining usable in isolated tests."""

    client = getattr(request.app.state, "rag_http_client", None)
    return RagServiceClient(settings, client=client)


def _successful_context_update(response: dict[str, object]) -> dict[str, object] | None:
    """Accept semantic memory only from verified, non-empty RAG answers."""

    update = response.get("conversationContextUpdate")
    if (
        response.get("status") != "ANSWERED"
        or response.get("confidence") == "NONE"
        or not isinstance(update, dict)
    ):
        return None
    return update


@router.get(
    "/{project_id}/conversations",
    response_model=list[ConversationSummaryResponse],
    response_model_by_alias=True,
)
async def list_project_conversations(
    project_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    conversation_store: MongoConversationStore = Depends(get_conversation_store),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConversationSummaryResponse]:
    """Restore one page of a signed-in user's conversations for an authorized project."""

    await _require_project_access(project_id, principal.object_id, access_reader)
    try:
        conversations = await conversation_store.list_conversations(
            owner_id=principal.object_id,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )
    except PyMongoError as failure:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Conversation memory is temporarily unavailable.",
        ) from failure
    return [_conversation_summary_response(item) for item in conversations]


@router.post(
    "/{project_id}/conversations",
    response_model=ConversationSummaryResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_conversation(
    project_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    conversation_store: MongoConversationStore = Depends(get_conversation_store),
) -> ConversationSummaryResponse:
    """Create one unique MongoDB conversation for an explicit New Chat action."""

    await _require_project_access(project_id, principal.object_id, access_reader)
    try:
        conversation = await conversation_store.create_conversation(
            owner_id=principal.object_id,
            project_id=project_id,
        )
    except PyMongoError as failure:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "A new conversation could not be created.",
        ) from failure
    return _conversation_summary_response(conversation)


@router.get(
    "/{project_id}/conversations/{conversation_id}",
    response_model=ConversationHistoryResponse,
    response_model_by_alias=True,
)
async def get_project_conversation(
    project_id: str,
    conversation_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    conversation_store: MongoConversationStore = Depends(get_conversation_store),
) -> ConversationHistoryResponse:
    """Restore completed messages from one strictly owner-scoped conversation."""

    await _require_project_access(project_id, principal.object_id, access_reader)
    try:
        history = await conversation_store.get_conversation(
            owner_id=principal.object_id,
            project_id=project_id,
            conversation_id=conversation_id,
        )
    except ConversationNotFoundError as failure:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(failure)) from failure
    except PyMongoError as failure:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Conversation memory is temporarily unavailable.",
        ) from failure
    return _conversation_history_response(history)


@router.post(
    "/{project_id}/chat",
    response_model=ChatResponse,
    response_model_by_alias=True,
)
async def project_chat(
    project_id: str,
    body: ChatRequest,
    request: Request,
    raw_response: Response,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: ProjectStore = Depends(get_project_store),
    conversation_store: MongoConversationStore = Depends(get_conversation_store),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    """Authorize a question and return one buffered project answer."""

    began = started()
    correlation_id = request_id(request.headers.get("x-request-id"))
    raw_response.headers["X-Request-ID"] = correlation_id
    user_hash = pseudonymous_user(principal.object_id, settings.telemetry_hmac_key)
    model_profile = _model_profile(body.mode)
    access = await access_reader.read(principal.object_id)
    if project_id not in access.projects or project_id not in access.project_roles:
        chat_event(
            request_id_value=correlation_id,
            project_id=project_id,
            user_hash=user_hash,
            began=began,
            status_code=403,
            reason_code="PROJECT_ACCESS_DENIED",
            model_profile=model_profile,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this project.")
    project = await project_store.get(project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The project is not configured.")
    enabled_providers = _enabled_providers(project, body.enabled_providers)

    # The backend, never the client, creates the retrieval policies.
    policies = _retrieval_policies(
        project_id,
        principal.object_id,
        access.project_roles[project_id],
        access.project_departments.get(project_id, ()),
    )
    pending = await _begin_conversation_turn(
        conversation_store, principal.object_id, project_id, body
    )
    try:
        response = await _rag_service_client(request, settings).answer(
            project_id,
            body.question,
            policies,
            project.vector_store.collection_name,
            project.vector_store.text_field,
            project.vector_store.embedding_field,
            project.vector_store.embedding_model,
            project.vector_store.schema_version,
            model_profile,
            correlation_id,
            tuple(
                {"role": message.role, "content": message.content} for message in pending.history
            ),
            pending.context.as_payload(),
            **(
                {"retrieval_profile": project.retrieval_profile.as_payload()}
                if getattr(project, "retrieval_profile", None) is not None
                else {}
            ),
            enabled_providers=enabled_providers,
        )
    except (httpx.HTTPError, ValueError, KeyError) as failure:
        chat_event(
            request_id_value=correlation_id,
            project_id=project_id,
            user_hash=user_hash,
            began=began,
            status_code=503,
            reason_code="RAG_UNAVAILABLE",
            model_profile=model_profile,
        )
        await conversation_store.abandon_turn(
            pending, owner_id=principal.object_id, project_id=project_id
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The project knowledge service is temporarily unavailable.",
        ) from failure

    result = _chat_response(project_id, pending.conversation_id, response)
    await conversation_store.complete_turn(
        pending,
        owner_id=principal.object_id,
        project_id=project_id,
        answer=result.answer,
        confidence=result.confidence,
        context_update=_successful_context_update(response),
    )
    chat_event(
        request_id_value=correlation_id,
        project_id=project_id,
        user_hash=user_hash,
        began=began,
        status_code=200,
        reason_code="OK" if result.confidence != "NONE" else "NO_ANSWER",
        model_profile=model_profile,
    )
    return result


@router.post("/{project_id}/chat/stream")
async def project_chat_stream(
    project_id: str,
    body: ChatRequest,
    request: Request,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: ProjectStore = Depends(get_project_store),
    conversation_store: MongoConversationStore = Depends(get_conversation_store),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Authorize once and stream safe progress plus verified answer text as NDJSON."""

    began = started()
    correlation_id = request_id(request.headers.get("x-request-id"))
    user_hash = pseudonymous_user(principal.object_id, settings.telemetry_hmac_key)
    model_profile = _model_profile(body.mode)
    access = await access_reader.read(principal.object_id)
    if project_id not in access.projects or project_id not in access.project_roles:
        chat_event(
            request_id_value=correlation_id,
            project_id=project_id,
            user_hash=user_hash,
            began=began,
            status_code=403,
            reason_code="PROJECT_ACCESS_DENIED",
            model_profile=model_profile,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this project.")
    project = await project_store.get(project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The project is not configured.")
    enabled_providers = _enabled_providers(project, body.enabled_providers)
    policies = _retrieval_policies(
        project_id,
        principal.object_id,
        access.project_roles[project_id],
        access.project_departments.get(project_id, ()),
    )
    pending = await _begin_conversation_turn(
        conversation_store, principal.object_id, project_id, body
    )

    async def events() -> AsyncIterator[str]:
        """Forward only the explicit public stream schema and record completion."""

        reason_code = "RAG_UNAVAILABLE"
        status_code = 503
        completed = False
        try:
            async for event in _rag_service_client(request, settings).stream_answer(
                project_id,
                body.question,
                policies,
                project.vector_store.collection_name,
                project.vector_store.text_field,
                project.vector_store.embedding_field,
                project.vector_store.embedding_model,
                project.vector_store.schema_version,
                model_profile,
                correlation_id,
                tuple(
                    {"role": message.role, "content": message.content}
                    for message in pending.history
                ),
                pending.context.as_payload(),
                **(
                    {"retrieval_profile": project.retrieval_profile.as_payload()}
                    if getattr(project, "retrieval_profile", None) is not None
                    else {}
                ),
                enabled_providers=enabled_providers,
            ):
                event_type = event.get("type")
                if event_type == "complete" and isinstance(event.get("response"), dict):
                    private_response = event["response"]
                    result = _chat_response(project_id, pending.conversation_id, private_response)
                    await conversation_store.complete_turn(
                        pending,
                        owner_id=principal.object_id,
                        project_id=project_id,
                        answer=result.answer,
                        confidence=result.confidence,
                        context_update=_successful_context_update(private_response),
                    )
                    completed = True
                    reason_code = "OK" if result.confidence != "NONE" else "NO_ANSWER"
                    status_code = 200
                    event = {
                        "type": "complete",
                        "response": result.model_dump(by_alias=True),
                    }
                elif event_type == "error":
                    reason_code = "RAG_STREAM_ERROR"
                elif event_type not in {
                    "status",
                    "answer_start",
                    "answer_delta",
                    "answer_snapshot",
                    "answer_sentence_verified",
                    "answer_sentence_rejected",
                }:
                    continue
                yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError):
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "message": "I couldn't complete that project search right now. Please try again.",
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        finally:
            if not completed:
                await conversation_store.abandon_turn(
                    pending, owner_id=principal.object_id, project_id=project_id
                )
            chat_event(
                request_id_value=correlation_id,
                project_id=project_id,
                user_hash=user_hash,
                began=began,
                status_code=status_code,
                reason_code=reason_code,
                model_profile=model_profile,
            )

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "X-Request-ID": correlation_id,
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-store",
        },
    )


async def _begin_conversation_turn(
    store: MongoConversationStore,
    owner_id: str,
    project_id: str,
    body: ChatRequest,
) -> PendingTurn:
    """Persist a scoped user turn and translate Mongo failures into safe API errors."""

    try:
        return await store.begin_turn(
            owner_id=owner_id,
            project_id=project_id,
            question=body.question,
            conversation_id=body.conversation_id,
        )
    except ConversationNotFoundError as failure:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(failure)) from failure
    except PyMongoError as failure:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Conversation memory is temporarily unavailable.",
        ) from failure


async def _require_project_access(
    project_id: str,
    owner_id: str,
    access_reader: GraphProjectAccessReader,
) -> None:
    access = await access_reader.read(owner_id)
    if project_id not in access.projects or project_id not in access.project_roles:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You do not have access to this project.",
        )

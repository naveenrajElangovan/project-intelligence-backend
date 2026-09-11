import json
from collections.abc import AsyncIterator

import httpx

from app.config import Settings


class RagServiceClient:
    """Call the private RAG service without exposing its credentials to clients."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def answer(
        self,
        project_id: str,
        question: str,
        access_policy_ids: tuple[str, ...],
        collection_name: str,
        text_field: str,
        embedding_field: str,
        embedding_model: str,
        schema_version: str,
        model_profile: str,
        request_id: str = "",
        conversation_history: tuple[dict[str, str], ...] = (),
        conversation_context: dict[str, object] | None = None,
        retrieval_profile: dict[str, int | float] | None = None,
        evaluation: bool = False,
    ) -> dict[str, object]:
        """Request one buffered, verified answer from the RAG service."""

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._settings.rag_request_timeout_seconds
        )
        try:
            headers = {}
            if self._settings.rag_internal_api_key:
                headers["Authorization"] = f"Bearer {self._settings.rag_internal_api_key}"
            if request_id:
                headers["X-Request-ID"] = request_id
            payload = {
                "projectId": project_id,
                "collectionName": collection_name,
                "textField": text_field,
                "embeddingField": embedding_field,
                "embeddingModel": embedding_model,
                "schemaVersion": schema_version,
                "question": question,
                "accessPolicyIds": list(access_policy_ids),
                "modelProfile": model_profile,
                "conversationHistory": list(conversation_history),
                "conversationContext": conversation_context or {},
            }
            if retrieval_profile is not None:
                payload["retrievalProfile"] = retrieval_profile
            if evaluation:
                payload["evaluation"] = True
            response = await client.post(
                f"{self._settings.rag_service_url.rstrip('/')}/v1/answer",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("RAG service returned a malformed response.")
            return body
        finally:
            if owns_client:
                await client.aclose()

    async def verify_evaluation_snapshot(self, target) -> None:
        """Recheck the operator-pinned inventory without returning source text."""
        async with httpx.AsyncClient(timeout=65) as client:
            response = await client.post(
                f"{self._settings.rag_service_url.rstrip('/')}/v1/internal/evaluation-snapshot",
                headers={"Authorization": f"Bearer {self._settings.rag_internal_api_key}"},
                json={"project_id": target.project_id, "ingestion_run_id": target.ingestion_run_id},
            )
            response.raise_for_status()
            snapshot = response.json()
            for field in (
                "inventory_sha256",
                "contract_sha256",
                "embedding_model",
                "schema_version",
            ):
                if snapshot.get(field) != getattr(target, field):
                    raise ValueError("Staging inventory changed after target registration.")

    async def stream_answer(
        self,
        project_id: str,
        question: str,
        access_policy_ids: tuple[str, ...],
        collection_name: str,
        text_field: str,
        embedding_field: str,
        embedding_model: str,
        schema_version: str,
        model_profile: str,
        request_id: str = "",
        conversation_history: tuple[dict[str, str], ...] = (),
        conversation_context: dict[str, object] | None = None,
        retrieval_profile: dict[str, int | float] | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        """Proxy verified NDJSON events while retaining backend-created policies."""

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                self._settings.rag_request_timeout_seconds,
                connect=min(15.0, self._settings.rag_request_timeout_seconds),
            )
        )
        headers = {"Accept": "application/x-ndjson"}
        if self._settings.rag_internal_api_key:
            headers["Authorization"] = f"Bearer {self._settings.rag_internal_api_key}"
        if request_id:
            headers["X-Request-ID"] = request_id
        payload = {
            "projectId": project_id,
            "collectionName": collection_name,
            "textField": text_field,
            "embeddingField": embedding_field,
            "embeddingModel": embedding_model,
            "schemaVersion": schema_version,
            "question": question,
            "accessPolicyIds": list(access_policy_ids),
            "modelProfile": model_profile,
            "conversationHistory": list(conversation_history),
            "conversationContext": conversation_context or {},
        }
        if retrieval_profile is not None:
            payload["retrievalProfile"] = retrieval_profile
        try:
            async with client.stream(
                "POST",
                f"{self._settings.rag_service_url.rstrip('/')}/v1/answer/stream",
                headers=headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                        raise ValueError("RAG service returned a malformed stream event.")
                    yield event
        finally:
            if owns_client:
                await client.aclose()

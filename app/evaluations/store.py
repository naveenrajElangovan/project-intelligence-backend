from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.infrastructure.sql.models import EvaluationRunRecord


class EvaluationRunStore:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def create(self, record: EvaluationRunRecord) -> None:
        async with self._sessions() as session, session.begin():
            session.add(record)

    async def get(self, run_id: str) -> EvaluationRunRecord | None:
        async with self._sessions() as session:
            return await session.get(EvaluationRunRecord, run_id)

    async def mark_enqueue_failed(self, run_id: str, reason: str) -> None:
        async with self._sessions() as session, session.begin():
            record = await session.get(EvaluationRunRecord, run_id)
            if record is not None:
                record.status = "QUEUE_FAILED"
                record.typed_failures = [{"type": "DEPENDENCY_FAILURE", "reason": reason}]
                record.updated_at = datetime.now(UTC)

    async def complete(
        self, run_id: str, *, status: str, phoenix_reference: str | None,
        aggregate_scores: dict[str, object], typed_failures: list[dict[str, object]],
    ) -> EvaluationRunRecord | None:
        async with self._sessions() as session, session.begin():
            record = await session.get(EvaluationRunRecord, run_id)
            if record is None:
                return None
            record.status = status
            record.phoenix_experiment_reference = phoenix_reference
            record.aggregate_scores = aggregate_scores
            record.typed_failures = typed_failures
            record.updated_at = datetime.now(UTC)
            return record

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal
from uuid import uuid4
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.dependencies import CurrentPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.graph import GraphProjectAccessReader
from app.config import Settings, get_settings
from app.evaluations.queue import EvaluationQueue
from app.evaluations.store import EvaluationRunStore
from app.evaluations.staging import StagingStore, execute_case, resolve_target
from app.application.rag_client import RagServiceClient
from app.projects.dependencies import get_project_store
from app.projects.store import ProjectStore
from app.infrastructure.sql.database import get_session_factory
from app.infrastructure.sql.models import EvaluationRunRecord

router = APIRouter(prefix="/v1", tags=["evaluations"])


class BoundedCase(BaseModel):
    case_id: str = Field(alias="caseId", min_length=1, max_length=160)
    question: str = Field(min_length=2, max_length=4000)
    reviewed_reference: str = Field(default="", alias="reviewedReference", max_length=30000)
    expected_sources: list[str] = Field(
        default_factory=list, alias="expectedSources", max_length=100
    )
    language: Literal["en", "es"]
    persona: str = Field(default="unspecified", max_length=80)


class CandidateConfiguration(BaseModel):
    """Only tunable model/retrieval values; never an endpoint or credential."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    prompt_version: str = Field(alias="promptVersion", min_length=1, max_length=160)
    model_version: str = Field(alias="modelVersion", min_length=1, max_length=160)
    model_profile: Literal["budget", "standard", "complex"] = Field(alias="modelProfile")
    retrieval_top_k: int = Field(alias="retrievalTopK", ge=1, le=100)
    rerank_top_n: int = Field(alias="rerankTopN", ge=1, le=50)
    retrieval_score_threshold: float = Field(alias="retrievalScoreThreshold", ge=0, le=1)


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    project_id: str = Field(alias="projectId", min_length=1, max_length=100)
    dataset_reference: str | None = Field(default=None, alias="datasetReference", max_length=500)
    cases: list[BoundedCase] = Field(default_factory=list, max_length=100)
    evaluator_suite_version: str = Field(alias="evaluatorSuiteVersion", min_length=1, max_length=80)
    candidate_configuration: CandidateConfiguration = Field(alias="candidateConfiguration")

    @model_validator(mode="after")
    def exactly_one_input(self) -> "EvaluationRequest":
        if bool(self.dataset_reference) == bool(self.cases):
            raise ValueError("provide exactly one of datasetReference or cases")
        return self


class EvaluationAccepted(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    run_id: str = Field(alias="runId")
    status: str


class EvaluationStatus(EvaluationAccepted):
    phoenix_experiment_reference: str | None = Field(alias="phoenixExperimentReference")
    aggregate_scores: dict[str, object] = Field(alias="aggregateScores")
    typed_failures: list[dict[str, object]] = Field(alias="typedFailures")


class EvaluationWorkerUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    status: Literal["RUNNING", "COMPLETED", "FAILED", "DEAD_LETTERED"]
    phoenix_experiment_reference: str | None = Field(
        default=None, alias="phoenixExperimentReference", max_length=500
    )
    aggregate_scores: dict[str, float] = Field(default_factory=dict, alias="aggregateScores")
    typed_failures: list[dict[str, object]] = Field(
        default_factory=list, alias="typedFailures", max_length=500
    )


EVALUATOR_SUITES = {
    "2026-09-10.1": {
        "version": "2026-09-10.1",
        "deterministic": [
            "authorization_leakage",
            "citation_validity",
            "requested_language",
            "response_completeness",
            "refusal_typing",
            "source_coverage",
            "latency_budget",
            "dependency_classification",
        ],
        "semantic": [
            "groundedness",
            "answer_relevance",
            "completeness",
            "reference_correctness",
            "refusal_appropriateness",
        ],
    }
}


def get_run_store() -> EvaluationRunStore:
    return EvaluationRunStore(get_session_factory())


def get_staging_store() -> StagingStore:
    return StagingStore(get_session_factory())


def get_evaluation_rag(settings: Annotated[Settings, Depends(get_settings)]) -> RagServiceClient:
    return RagServiceClient(settings)


@lru_cache
def get_evaluation_queue() -> EvaluationQueue:
    return EvaluationQueue(get_settings())


async def _authorize(project_id: str, principal, reader: GraphProjectAccessReader) -> None:
    access = await reader.read(principal.object_id)
    if project_id not in access.projects or project_id not in access.project_roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this project.")


@router.post(
    "/evaluations", response_model=EvaluationAccepted, response_model_by_alias=True, status_code=202
)
async def create_evaluation(
    request: EvaluationRequest,
    principal: CurrentPrincipal,
    reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    store: Annotated[EvaluationRunStore, Depends(get_run_store)],
    queue: Annotated[EvaluationQueue, Depends(get_evaluation_queue)],
    targets: Annotated[StagingStore, Depends(get_staging_store)],
    projects: Annotated[ProjectStore, Depends(get_project_store)],
) -> EvaluationAccepted:
    await _authorize(request.project_id, principal, reader)
    if request.evaluator_suite_version not in EVALUATOR_SUITES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown evaluator suite version."
        )
    run_id = str(uuid4())
    now = datetime.now(UTC)
    payload = {"schema_version": "1", "run_id": run_id, **request.model_dump(by_alias=False)}
    state = "QUEUED"
    if request.dataset_reference and request.dataset_reference.startswith("staging:"):
        project = await projects.get(request.project_id)
        if project is None:
            raise HTTPException(404, "The project is not configured.")
        target_id = request.dataset_reference.removeprefix("staging:")
        target = await resolve_target(targets, target_id, project)
        payload.update(staging_target_id=target_id, staging_manifest_sha256=target.fingerprint)
        # Staging cases execute explicitly under the submitting user's current
        # bearer token. Never send them to a service-only legacy queue consumer.
        state = "READY"
    await store.create(
        EvaluationRunRecord(
            run_id=run_id,
            project_id=request.project_id,
            requested_by=principal.object_id,
            status=state,
            evaluator_suite_version=request.evaluator_suite_version,
            request_payload=payload,
            aggregate_scores={},
            typed_failures=[],
            created_at=now,
            updated_at=now,
        )
    )
    if state == "READY":
        return EvaluationAccepted(runId=run_id, status=state)
    try:
        await queue.send(payload)
    except Exception as failure:
        await store.mark_enqueue_failed(run_id, type(failure).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Evaluation queue is unavailable."
        ) from failure
    return EvaluationAccepted(runId=run_id, status="QUEUED")


@router.post("/evaluations/{run_id}/cases/{case_id}")
async def execute_staging_case(
    run_id: str,
    case_id: str,
    principal: CurrentPrincipal,
    reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    runs: Annotated[EvaluationRunStore, Depends(get_run_store)],
    targets: Annotated[StagingStore, Depends(get_staging_store)],
    projects: Annotated[ProjectStore, Depends(get_project_store)],
    rag: Annotated[RagServiceClient, Depends(get_evaluation_rag)],
) -> dict[str, object]:
    record = await runs.get(run_id)
    if record is None:
        raise HTTPException(404, "Evaluation run was not found.")
    return await execute_case(
        principal=principal,
        run=record,
        case_id=case_id,
        reader=reader,
        projects=projects,
        store=targets,
        rag=rag,
    )


@router.get("/evaluations/{run_id}", response_model=EvaluationStatus, response_model_by_alias=True)
async def evaluation_status(
    run_id: str,
    principal: CurrentPrincipal,
    reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    store: Annotated[EvaluationRunStore, Depends(get_run_store)],
) -> EvaluationStatus:
    record = await store.get(run_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evaluation run was not found.")
    await _authorize(record.project_id, principal, reader)
    return EvaluationStatus(
        runId=record.run_id,
        status=record.status,
        phoenixExperimentReference=record.phoenix_experiment_reference,
        aggregateScores=record.aggregate_scores,
        typedFailures=record.typed_failures,
    )


@router.get("/evaluator-suites/{version}")
async def evaluator_suite(version: str, principal: CurrentPrincipal) -> dict[str, object]:
    del principal
    suite = EVALUATOR_SUITES.get(version)
    if suite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evaluator suite was not found.")
    return dict(suite)


@router.put("/internal/evaluations/{run_id}", include_in_schema=False, status_code=204)
async def update_evaluation_from_worker(
    run_id: str,
    update: EvaluationWorkerUpdate,
    store: Annotated[EvaluationRunStore, Depends(get_run_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    internal_key: Annotated[str | None, Header(alias="X-Internal-API-Key")] = None,
) -> None:
    configured = settings.evaluation_internal_api_key
    if not configured or not internal_key or not secrets.compare_digest(configured, internal_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Worker authentication is required.")
    record = await store.complete(
        run_id,
        status=update.status,
        phoenix_reference=update.phoenix_experiment_reference,
        aggregate_scores=dict(update.aggregate_scores),
        typed_failures=update.typed_failures,
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evaluation run was not found.")

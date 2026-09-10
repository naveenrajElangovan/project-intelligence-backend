from fastapi.testclient import TestClient

from app.api.evaluations import get_evaluation_queue, get_run_store
from app.auth.dependencies import get_current_principal
from app.auth.models import EntraPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.models import ProjectAccessContext
from app.main import app
from app.config import Settings, get_settings


def _principal() -> EntraPrincipal:
    return EntraPrincipal("user-1", "subject", "tenant", "Developer", "dev@example.com", None, {})


class AccessReader:
    def __init__(self, projects=("T2.0",)):
        self.projects = projects

    async def read(self, user_object_id):
        assert user_object_id == "user-1"
        return ProjectAccessContext(self.projects, {project: ("DEVELOPER",) for project in self.projects})


class Store:
    def __init__(self):
        self.records = {}

    async def create(self, record):
        self.records[record.run_id] = record

    async def get(self, run_id):
        return self.records.get(run_id)

    async def mark_enqueue_failed(self, run_id, reason):
        self.records[run_id].status = "QUEUE_FAILED"

    async def complete(self, run_id, **values):
        record = self.records.get(run_id)
        if record is None:
            return None
        record.status = values["status"]
        record.phoenix_experiment_reference = values["phoenix_reference"]
        record.aggregate_scores = values["aggregate_scores"]
        record.typed_failures = values["typed_failures"]
        return record


class Queue:
    def __init__(self):
        self.messages = []

    async def send(self, payload):
        self.messages.append(payload)


def _overrides(projects=("T2.0",)):
    store, queue = Store(), Queue()
    app.dependency_overrides[get_current_principal] = _principal
    app.dependency_overrides[get_graph_project_access_reader] = lambda: AccessReader(projects)
    app.dependency_overrides[get_run_store] = lambda: store
    app.dependency_overrides[get_evaluation_queue] = lambda: queue
    return store, queue


def test_evaluation_submission_is_project_authorized_and_queued() -> None:
    store, queue = _overrides()
    try:
        response = TestClient(app).post(
            "/v1/evaluations",
            headers={"Authorization": "Bearer test"},
            json={
                "projectId": "T2.0",
                "datasetReference": "rag-gold-project-T2.0-v1",
                "evaluatorSuiteVersion": "2026-09-10.1",
                "candidateConfiguration": {
                    "promptVersion": "abc", "modelVersion": "qwen3.5",
                    "modelProfile": "standard", "retrievalTopK": 25,
                    "rerankTopN": 8, "retrievalScoreThreshold": 0.0,
                },
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    assert len(store.records) == len(queue.messages) == 1
    assert queue.messages[0]["project_id"] == "T2.0"


def test_evaluation_submission_rejects_another_project() -> None:
    _overrides(projects=("AAOS",))
    try:
        response = TestClient(app).post(
            "/v1/evaluations",
            headers={"Authorization": "Bearer test"},
            json={
                "projectId": "T2.0",
                "datasetReference": "dataset-1",
                "evaluatorSuiteVersion": "2026-09-10.1",
                "candidateConfiguration": {
                    "promptVersion": "abc", "modelVersion": "qwen3.5",
                    "modelProfile": "standard", "retrievalTopK": 25,
                    "rerankTopN": 8, "retrievalScoreThreshold": 0.0,
                },
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 403


def test_evaluation_requires_exactly_one_bounded_input() -> None:
    _overrides()
    try:
        response = TestClient(app).post(
            "/v1/evaluations",
            headers={"Authorization": "Bearer test"},
            json={
                "projectId": "T2.0",
                "evaluatorSuiteVersion": "2026-09-10.1",
                "candidateConfiguration": {
                    "promptVersion": "abc", "modelVersion": "qwen3.5",
                    "modelProfile": "standard", "retrievalTopK": 25,
                    "rerankTopN": 8, "retrievalScoreThreshold": 0.0,
                },
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422


def test_evaluator_suite_is_discoverable_but_authenticated() -> None:
    _overrides()
    try:
        response = TestClient(app).get(
            "/v1/evaluator-suites/2026-09-10.1",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "authorization_leakage" in response.json()["deterministic"]


def test_worker_callback_requires_service_credential_and_updates_run() -> None:
    store, _ = _overrides()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, evaluation_internal_api_key="e" * 32
    )
    from app.infrastructure.sql.models import EvaluationRunRecord
    from datetime import UTC, datetime
    record = EvaluationRunRecord(
        run_id="run-1", project_id="T2.0", requested_by="user-1", status="QUEUED",
        evaluator_suite_version="2026-09-10.1", request_payload={},
        aggregate_scores={}, typed_failures=[], created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    store.records["run-1"] = record
    try:
        denied = TestClient(app).put(
            "/v1/internal/evaluations/run-1", json={"status": "COMPLETED"}
        )
        accepted = TestClient(app).put(
            "/v1/internal/evaluations/run-1",
            headers={"X-Internal-API-Key": "e" * 32},
            json={
                "status": "COMPLETED", "phoenixExperimentReference": "experiment-1",
                "aggregateScores": {"groundedness": 0.95}, "typedFailures": [],
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert denied.status_code == 401
    assert accepted.status_code == 204
    assert record.phoenix_experiment_reference == "experiment-1"

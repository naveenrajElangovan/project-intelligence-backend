from fastapi.testclient import TestClient

from app import metrics
from app.main import app

client = TestClient(app)


def _sample(exposition: str, series: str) -> float | None:
    for line in exposition.splitlines():
        if line.startswith(series):
            return float(line.rsplit(" ", 1)[1])
    return None


def test_metrics_exposes_every_dependency_series_before_any_check() -> None:
    metrics.initialize()

    body = client.get("/metrics").text

    # An alert on == 0 is silent until the series exists, so both must be present
    # from startup rather than appearing on first readiness check.
    assert _sample(body, 'pi_backend_dependency_ready{dependency="database"}') == 1.0
    assert _sample(body, 'pi_backend_dependency_ready{dependency="rag"}') == 1.0


def test_metrics_content_type_is_prometheus_exposition() -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_metrics_is_absent_when_disabled(monkeypatch) -> None:
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "metrics_enabled", False)

    assert client.get("/metrics").status_code == 404


def test_dependency_failure_is_recorded_per_dependency() -> None:
    metrics.initialize()

    metrics.record_dependency_failure("database")
    body = client.get("/metrics").text

    assert _sample(body, 'pi_backend_dependency_ready{dependency="database"}') == 0.0
    # A failing SQL check must not implicate RAG, which is what the blackbox
    # probe of /ready could not distinguish.
    assert _sample(body, 'pi_backend_dependency_ready{dependency="rag"}') == 1.0
    assert (
        _sample(body, 'pi_backend_dependency_check_failures_total{dependency="database"}')
        == 1.0
    )

    metrics.record_dependency_ready("database")


def test_token_outcomes_are_counted_separately() -> None:
    metrics.initialize()

    metrics.record_token_acquired()
    metrics.record_token_failure()
    body = client.get("/metrics").text

    assert _sample(body, 'pi_backend_sql_access_token_total{outcome="acquired"}') >= 1.0
    assert _sample(body, 'pi_backend_sql_access_token_total{outcome="failed"}') >= 1.0


def test_exposition_carries_no_identifiers() -> None:
    body = client.get("/metrics").text

    for forbidden in ("project_id", "user", "question", "token=", "password"):
        assert forbidden not in body


def test_series_exist_without_an_explicit_initialize_call() -> None:
    # Startup aborts when MongoDB is unreachable, so the exposition must be
    # populated by import alone or the dependency alerts stay silent in exactly
    # the outage they exist to report.
    import importlib

    module = importlib.reload(importlib.import_module("app.metrics"))

    exposition = module.render().decode()
    assert 'pi_backend_dependency_ready{dependency="database"}' in exposition
    assert 'pi_backend_dependency_ready{dependency="rag"}' in exposition

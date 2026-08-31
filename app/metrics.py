"""Prometheus metrics for the backend control plane.

Every series here is operational only: dependency reachability, Azure SQL token
acquisition outcomes and connection-pool occupancy. No project identifier, user
identifier, question, answer or credential is ever recorded, so the exposition
carries nothing that log redaction would have to strip.
"""

from __future__ import annotations

import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest


logger = logging.getLogger("app.metrics")

REGISTRY = CollectorRegistry()

DEPENDENCY_READY = Gauge(
    "pi_backend_dependency_ready",
    "Whether a backend dependency passed its last readiness check (1) or not (0).",
    ["dependency"],
    registry=REGISTRY,
)

DEPENDENCY_CHECK_FAILURES = Counter(
    "pi_backend_dependency_check_failures_total",
    "Readiness check failures by dependency.",
    ["dependency"],
    registry=REGISTRY,
)

SQL_ACCESS_TOKEN = Counter(
    "pi_backend_sql_access_token_total",
    "Azure SQL access token acquisition attempts by outcome.",
    ["outcome"],
    registry=REGISTRY,
)

SQL_POOL_CONNECTIONS = Gauge(
    "pi_backend_sql_pool_connections",
    "Azure SQL connection pool occupancy by state.",
    ["state"],
    registry=REGISTRY,
)

_DEPENDENCIES = ("database", "rag")
_TOKEN_OUTCOMES = ("acquired", "failed")


def initialize() -> None:
    """Create every child series so absence is distinguishable from zero.

    Without this an alert on ``pi_backend_dependency_ready == 0`` stays silent
    until the first check runs, because the series does not yet exist. Called at
    import rather than only from the lifespan hook: startup aborts if MongoDB is
    unreachable, which is exactly when the exposition must still be scrapable.
    Idempotent, so the lifespan call remains harmless.
    """

    for dependency in _DEPENDENCIES:
        DEPENDENCY_READY.labels(dependency=dependency).set(1)
        DEPENDENCY_CHECK_FAILURES.labels(dependency=dependency)
    for outcome in _TOKEN_OUTCOMES:
        SQL_ACCESS_TOKEN.labels(outcome=outcome)


def record_dependency_ready(dependency: str) -> None:
    DEPENDENCY_READY.labels(dependency=dependency).set(1)


def record_dependency_failure(dependency: str) -> None:
    DEPENDENCY_READY.labels(dependency=dependency).set(0)
    DEPENDENCY_CHECK_FAILURES.labels(dependency=dependency).inc()


def record_token_acquired() -> None:
    SQL_ACCESS_TOKEN.labels(outcome="acquired").inc()


def record_token_failure() -> None:
    SQL_ACCESS_TOKEN.labels(outcome="failed").inc()


def _refresh_pool_gauges() -> None:
    """Sample pool occupancy at scrape time rather than tracking it per checkout."""

    from app.infrastructure.sql.database import get_engine

    pool = get_engine().sync_engine.pool
    for state, reader in (
        ("in_use", getattr(pool, "checkedout", None)),
        ("available", getattr(pool, "checkedin", None)),
        ("overflow", getattr(pool, "overflow", None)),
    ):
        if reader is None:
            continue
        SQL_POOL_CONNECTIONS.labels(state=state).set(reader())


def render() -> bytes:
    try:
        _refresh_pool_gauges()
    except Exception as error:
        # A pool that cannot be sampled must not break the whole exposition;
        # dependency and token series remain useful on their own.
        logger.debug("Could not sample the Azure SQL pool: %s", error)
    return generate_latest(REGISTRY)


initialize()

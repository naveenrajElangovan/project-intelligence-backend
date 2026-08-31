from typing import Literal
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel
from sqlalchemy import text

from app import metrics
from app.config import Settings, get_settings
from app.infrastructure.sql.database import get_engine

router = APIRouter(tags=["Operations"])

logger = logging.getLogger("app.api.health")


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    environment: str


@router.get("/health", response_model=HealthResponse, summary="Check service health")
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="project-intelligence-backend",
        version=settings.app_version,
        environment=settings.environment,
    )


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(settings: Settings = Depends(get_settings)) -> Response:
    # Clients reach this service directly, unlike the private RAG service, so the
    # exposition is disabled unless an operator opts in and the TLS proxy is
    # configured to refuse /metrics from the internet.
    if not settings.metrics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)


# Last time a real SQL connection succeeded, and the outcome. A readiness probe
# is not a reason to open another one.
_LAST_SQL_PROBE: tuple[float, bool] = (0.0, False)


async def _verify_database() -> None:
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


async def _verify_database_readiness(settings: Settings) -> None:
    """Probe the database at most once per configured window.

    A blackbox probe hitting /ready every 15 seconds opened 5,760 SQL connections
    a day, which is what kept a serverless database permanently online: it bills
    vCore-seconds for being awake, not for doing work, and the free grant is only
    about 28 vCore-hours a month. Readiness is a property that changes on the
    scale of minutes, so re-verifying it on every scrape bought nothing and cost
    the entire monthly allowance.

    Set PI_READINESS_VERIFY_DATABASE=false to stop probing altogether, which is
    the right choice on a free-tier serverless database: real request traffic
    still surfaces a broken connection, with a named remedy from
    connection_refusal_remedy.
    """

    global _LAST_SQL_PROBE

    if not settings.readiness_verify_database:
        return
    probed_at, healthy = _LAST_SQL_PROBE
    age = time.monotonic() - probed_at
    if probed_at and age < settings.readiness_database_interval_seconds:
        if healthy:
            return
        # A cached failure is still a failure: report it rather than pretending
        # the window has not elapsed.
        raise RuntimeError(
            "The database was unreachable "
            f"{int(age)}s ago and has not been re-probed yet."
        )
    try:
        await _verify_database()
    except Exception:
        _LAST_SQL_PROBE = (time.monotonic(), False)
        raise
    _LAST_SQL_PROBE = (time.monotonic(), True)


async def _verify_rag(settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{settings.rag_service_url.rstrip('/')}/ready")
        response.raise_for_status()


@router.get("/ready", response_model=HealthResponse, summary="Check service dependencies")
async def ready(settings: Settings = Depends(get_settings)) -> HealthResponse:
    # Clients only ever receive the generic message below. The operator-facing
    # cause is logged and counted per dependency, because an unattributed 503 is
    # not diagnosable from either the service log or a blackbox probe.
    checks = (
        ("database", lambda: _verify_database_readiness(settings)),
        ("rag", lambda: _verify_rag(settings)),
    )
    for dependency, check in checks:
        try:
            await check()
        except Exception as error:
            metrics.record_dependency_failure(dependency)
            logger.warning(
                "Readiness check failed for %s: %s: %s",
                dependency,
                type(error).__name__,
                error,
                exc_info=True,
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "A required service dependency is unavailable.",
            ) from error
        metrics.record_dependency_ready(dependency)
    return HealthResponse(
        status="ok",
        service="project-intelligence-backend",
        version=settings.app_version,
        environment=settings.environment,
    )

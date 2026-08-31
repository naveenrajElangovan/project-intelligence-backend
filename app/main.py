from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.health import router as health_router
from app.api.chat import router as chat_router
from app.api.integrations import router as integrations_router
from app.api.internal_ingestion import router as internal_ingestion_router
from app.api.me import router as me_router
from app.api.project_configuration import router as project_configuration_router
from app.api.projects import router as projects_router
from app.config import get_settings
from app.conversations.dependencies import get_conversation_store
from app import metrics
from app.logging import configure_application_logging
from app.security import RequestSizeLimitMiddleware, SecurityHeadersMiddleware


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Initialize shared persistence and private-service connection pools."""

    settings = get_settings()
    metrics.initialize()
    store = get_conversation_store()
    await store.initialize()
    application.state.rag_http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.rag_request_timeout_seconds,
            connect=min(15.0, settings.rag_request_timeout_seconds),
        ),
        limits=httpx.Limits(
            max_connections=settings.rag_max_connections,
            max_keepalive_connections=settings.rag_max_keepalive_connections,
            keepalive_expiry=settings.rag_keepalive_expiry_seconds,
        ),
    )
    try:
        yield
    finally:
        await application.state.rag_http_client.aclose()
        await store.close()


def create_app() -> FastAPI:
    """Build the authenticated backend and initialize its persistence boundaries."""

    settings = get_settings()
    configure_application_logging(settings.log_level)
    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="REST API boundary for Project Intelligence clients.",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_host_list,
    )
    if settings.force_https:
        application.add_middleware(HTTPSRedirectMiddleware)
    application.add_middleware(
        RequestSizeLimitMiddleware,
        maximum_bytes=settings.max_request_body_bytes,
    )
    application.add_middleware(SecurityHeadersMiddleware, hsts=settings.force_https)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    application.include_router(health_router)
    application.include_router(me_router)
    application.include_router(project_configuration_router)
    application.include_router(integrations_router)
    application.include_router(internal_ingestion_router)
    application.include_router(projects_router)
    application.include_router(chat_router)
    return application
app = create_app()

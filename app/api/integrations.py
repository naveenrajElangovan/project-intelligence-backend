import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from app.api.integration_helpers import (
    callback_page,
    provider_secret_name,
    require_technical_lead_project,
)
from app.api.integration_contracts import (
    ConnectResponse,
    IntegrationStatusResponse,
    ProjectAccessContextResponse,
    ProjectSourceResponse,
    SynchronizationResponse,
)

from app.auth.dependencies import CurrentPrincipal
from app.auth.models import EntraPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.graph import GraphProjectAccessReader
from app.application.ports.secrets import SecretStore
from app.config import Settings, get_settings
from app.integrations.atlassian.client import AtlassianOAuthClient, AtlassianResource
from app.integrations.atlassian.service import register_session_if_configured
from app.integrations.dependencies import get_integration_store, get_secret_store
from app.integrations.models import OAuthState, ProjectSource, ProviderConnection
from app.integrations.store import IntegrationStore
from app.projects.dependencies import get_project_store
from app.projects.models import ProjectDefinition
from app.projects.store import ProjectStore

router = APIRouter(prefix="/v1", tags=["integrations"])


@router.get(
    "/projects/{project_id}/access-context",
    response_model=ProjectAccessContextResponse,
    response_model_by_alias=True,
)
async def project_access_context(
    project_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
    store: Annotated[IntegrationStore, Depends(get_integration_store)],
    secrets_store: Annotated[SecretStore, Depends(get_secret_store)],
    settings: Settings = Depends(get_settings),
) -> ProjectAccessContextResponse:
    project, role = await _require_project_access(
        project_id, principal, access_reader, project_store
    )
    integrations = await _integration_statuses(
        project, store, secrets_store, settings
    )
    return ProjectAccessContextResponse(
        project_id=project.project_id,
        display_name=project.display_name,
        role=role,
        authorized=True,
        authorization_mode="ENTRA_PROJECT_SCOPED",
        can_ask_questions=_can_ask_questions(project),
        integrations=integrations,
    )



def _can_ask_questions(project) -> bool:
    """Chat needs an active retrieval target; provider OAuth is ingestion-only."""

    return bool(
        project.active
        and project.vector_store.collection_name
    )


@router.get(
    "/projects/{project_id}/integrations",
    response_model=list[IntegrationStatusResponse],
    response_model_by_alias=True,
)
async def integration_statuses(
    project_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
    store: Annotated[IntegrationStore, Depends(get_integration_store)],
    secrets_store: Annotated[SecretStore, Depends(get_secret_store)],
    settings: Settings = Depends(get_settings),
) -> list[IntegrationStatusResponse]:
    project = await _require_project(project_id, principal, access_reader, project_store)
    return await _integration_statuses(project, store, secrets_store, settings)


async def _integration_statuses(
    project: ProjectDefinition,
    store: IntegrationStore,
    secrets_store: SecretStore,
    settings: Settings,
) -> list[IntegrationStatusResponse]:
    indexed = set(project.vector_store.indexed_providers)
    atlassian = await store.get_connection(project.project_id, "ATLASSIAN")
    connected = atlassian is not None and (
        not atlassian.token_is_expired or await _has_refresh_token(atlassian, secrets_store)
    )
    atlassian_message = (
        "Connected and synchronized."
        if connected and atlassian and atlassian.last_synchronized_at
        else "Connected. Synchronization has not started."
        if connected
        else "Authorization is required."
        if settings.atlassian_oauth_configured
        else "Provider configuration is incomplete."
    )
    def atlassian_status(provider: str, source_configured: bool) -> IntegrationStatusResponse:
        indexed_snapshot = provider in indexed
        configured = (settings.atlassian_oauth_configured and source_configured) or indexed_snapshot
        message = (
            f"No {provider.title()} source is mapped to this project."
            if not source_configured
            else atlassian_message
        )
        return IntegrationStatusResponse(
            provider=provider,
            configured=configured,
            connected=connected and settings.atlassian_oauth_configured and source_configured,
            available=(connected and settings.atlassian_oauth_configured and source_configured)
            or indexed_snapshot,
            resource_name=atlassian.resource_name if atlassian else None,
            resource_url=atlassian.resource_url if atlassian else None,
            connected_at=atlassian.connected_at if atlassian else None,
            last_synchronized_at=atlassian.last_synchronized_at if atlassian else None,
            message=message,
        )

    return [
        atlassian_status("JIRA", bool(project.jira_projects)),
        atlassian_status("CONFLUENCE", bool(project.confluence_spaces)),
        IntegrationStatusResponse(
            provider="GITHUB",
            configured=bool(project.github_repositories) or "GITHUB" in indexed,
            connected=bool(project.github_repositories),
            available=bool(project.github_repositories) or "GITHUB" in indexed,
            message=(
                "Repository mapping is stored in Azure SQL; ingestion runs in the separate service."
                if project.github_repositories
                else "Indexed local code is available; no live GitHub repository is configured."
                if "GITHUB" in indexed
                else "No GitHub repository is mapped to this project."
            ),
        ),
    ]


@router.post(
    "/projects/{project_id}/integrations/atlassian/synchronize",
    response_model=SynchronizationResponse,
    response_model_by_alias=True,
)
async def synchronize_atlassian(
    project_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
    store: Annotated[IntegrationStore, Depends(get_integration_store)],
    secrets_store: Annotated[SecretStore, Depends(get_secret_store)],
    settings: Settings = Depends(get_settings),
) -> SynchronizationResponse:
    project = await require_technical_lead_project(
        project_id, principal, access_reader, project_store
    )
    if not project.jira_projects and not project.confluence_spaces:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Jira and Confluence source mappings are not configured in Azure SQL.",
        )
    connection = await store.get_connection(project_id, "ATLASSIAN")
    if connection is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Connect Atlassian before synchronizing.")
    required_scopes = {"read:jira-work", "read:page:confluence", "read:attachment:confluence"}
    missing_scopes = required_scopes.difference(connection.scopes)
    if missing_scopes:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Reconnect Atlassian to grant the required Jira and Confluence permissions.",
        )

    client = AtlassianOAuthClient(settings)
    try:
        connection, access_token = await _valid_atlassian_connection(
            connection, client, store, secrets_store
        )
    except (httpx.HTTPError, ValueError) as failure:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "The Atlassian connection could not be refreshed."
        ) from failure

    try:
        jira_source_list: list[ProjectSource] = []
        for configured_source in project.jira_projects:
            jira_source_list.extend(
                await client.jira_sources(
                access_token,
                connection.resource_id,
                project_id,
                configured_source.project_key,
                configured_source.site_url,
            )
            )
        jira_sources = tuple(jira_source_list)
    except httpx.HTTPStatusError as failure:
        raise _provider_sync_error("Jira", failure) from failure
    except (httpx.HTTPError, ValueError) as failure:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Jira synchronization failed.") from failure

    try:
        confluence_source_list: list[ProjectSource] = []
        for configured_source in project.confluence_spaces:
            confluence_source_list.extend(
                await client.confluence_sources(
                access_token,
                connection.resource_id,
                project_id,
                configured_source.space_id,
                configured_source.site_url,
            )
            )
        confluence_sources = tuple(confluence_source_list)
    except httpx.HTTPStatusError as failure:
        raise _provider_sync_error("Confluence", failure) from failure
    except (httpx.HTTPError, ValueError) as failure:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Confluence synchronization failed."
        ) from failure

    synchronized_at = datetime.now(UTC)
    await store.save_synchronized_sources(
        project_id, "JIRA", jira_sources, synchronized_at
    )
    await store.save_synchronized_sources(
        project_id,
        "CONFLUENCE",
        confluence_sources,
        synchronized_at,
    )
    return SynchronizationResponse(
        project_id=project_id,
        synchronized_at=synchronized_at,
        jira_issues=len(jira_sources),
        confluence_pages=sum(source.source_type == "PAGE" for source in confluence_sources),
        confluence_attachments=sum(
            source.source_type == "ATTACHMENT" for source in confluence_sources
        ),
    )


def _provider_sync_error(provider: str, failure: httpx.HTTPStatusError) -> HTTPException:
    if failure.response.status_code in (401, 403):
        return HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"The connected Atlassian user cannot read the configured {provider} source.",
        )
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        f"{provider} synchronization failed with status {failure.response.status_code}.",
    )


@router.get(
    "/projects/{project_id}/sources",
    response_model=list[ProjectSourceResponse],
    response_model_by_alias=True,
)
async def project_sources(
    project_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
    store: Annotated[IntegrationStore, Depends(get_integration_store)],
    provider: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    settings: Settings = Depends(get_settings),
) -> list[ProjectSourceResponse]:
    await _require_project(project_id, principal, access_reader, project_store)
    normalized_provider = provider.upper() if provider else None
    if normalized_provider not in (None, "JIRA", "CONFLUENCE", "GITHUB"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unsupported provider.")
    sources = await store.list_authorized_sources(
        project_id, normalized_provider, limit
    )
    return [_source_response(source) for source in sources]


@router.post(
    "/projects/{project_id}/integrations/atlassian/connect",
    response_model=ConnectResponse,
    response_model_by_alias=True,
)
async def connect_atlassian(
    project_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
    store: Annotated[IntegrationStore, Depends(get_integration_store)],
    settings: Settings = Depends(get_settings),
) -> ConnectResponse:
    project = await require_technical_lead_project(
        project_id, principal, access_reader, project_store
    )
    if not settings.atlassian_oauth_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Atlassian OAuth is not configured.")
    if not project.jira_projects and not project.confluence_spaces:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Configure Jira or Confluence sources in Azure SQL before connecting Atlassian.",
        )

    raw_state = secrets.token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    await store.save_oauth_state(
        OAuthState(
            state_hash=_state_hash(raw_state),
            user_id=principal.object_id,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            provider="ATLASSIAN",
            expires_at=expires_at,
            user_email=principal.email or principal.username,
        )
    )
    query = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": settings.atlassian_client_id,
            "scope": settings.atlassian_scopes,
            "redirect_uri": settings.atlassian_redirect_uri,
            "state": raw_state,
            "response_type": "code",
            "prompt": "consent",
        }
    )
    return ConnectResponse(
        provider="ATLASSIAN",
        authorization_url=f"https://auth.atlassian.com/authorize?{query}",
        expires_at=expires_at,
    )


@router.get("/integrations/atlassian/callback", response_class=HTMLResponse)
async def atlassian_callback(
    state: Annotated[str | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
    store: IntegrationStore = Depends(get_integration_store),
    project_store: ProjectStore = Depends(get_project_store),
    secrets_store: SecretStore = Depends(get_secret_store),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    if error:
        return _callback_page("Connection cancelled", error_description or error, success=False)
    if not state or not code:
        return _callback_page("Connection failed", "The OAuth callback is incomplete.", success=False)

    oauth_state = await store.consume_oauth_state(_state_hash(state), "ATLASSIAN")
    if oauth_state is None:
        return _callback_page(
            "Connection expired",
            "Start the Atlassian connection again from Project Intelligence.",
            success=False,
        )

    client = AtlassianOAuthClient(settings)
    try:
        project = await project_store.get(oauth_state.project_id)
        if project is None:
            raise ValueError("The project is no longer configured in Azure SQL.")
        configured_sites = {
            source.site_url for source in [*project.jira_projects, *project.confluence_spaces]
        }
        if len(configured_sites) != 1:
            raise ValueError("The project must have exactly one configured Atlassian site.")
        credentials = await client.exchange_code(code)
        resources = await client.accessible_resources(credentials.access_token)
        resource = _select_resource(resources, next(iter(configured_sites)))
        identity = await client.current_user(credentials.access_token, resource.cloud_id)
        if settings.atlassian_require_email_match and not _same_email(
            oauth_state.user_email, identity.email
        ):
            raise ValueError(
                "The Atlassian account email must match the signed-in Microsoft account."
            )
        now = datetime.now(UTC)
        secret_reference = await secrets_store.put_json(
            _provider_secret_name(oauth_state.project_id, "atlassian"),
            {
                "accessToken": credentials.access_token,
                "refreshToken": credentials.refresh_token,
                "tokenType": credentials.token_type,
            }
        )
        await store.upsert_connection(
            ProviderConnection(
                connected_by=oauth_state.user_id,
                tenant_id=oauth_state.tenant_id,
                project_id=oauth_state.project_id,
                provider="ATLASSIAN",
                secret_reference=secret_reference,
                resource_id=resource.cloud_id,
                resource_url=resource.url,
                resource_name=resource.name,
                scopes=credentials.scopes or resource.scopes,
                token_expires_at=credentials.expires_at,
                connected_at=now,
                updated_at=now,
                provider_account_id=identity.account_id,
                provider_display_name=identity.display_name,
                provider_email=identity.email,
            )
        )
        await register_session_if_configured(
            settings, user_id=oauth_state.user_id, project_id=oauth_state.project_id,
            access_token=credentials.access_token, expires_at=credentials.expires_at,
        )
    except (httpx.HTTPError, ValueError):
        return _callback_page(
            "Connection failed",
            "The Atlassian connection could not be completed. Start again from the application.",
            success=False,
        )

    return _callback_page(
        "Atlassian connected",
        "Jira and Confluence are now authorized. You can return to Project Intelligence.",
        success=True,
    )


async def _require_project(
    project_id: str,
    principal: EntraPrincipal,
    access_reader: GraphProjectAccessReader,
    project_store: ProjectStore,
) -> ProjectDefinition:
    project, _ = await _require_project_access(
        project_id, principal, access_reader, project_store
    )
    return project


async def _require_project_access(
    project_id: str,
    principal: EntraPrincipal,
    access_reader: GraphProjectAccessReader,
    project_store: ProjectStore,
) -> tuple[ProjectDefinition, str]:
    context = await access_reader.read(principal.object_id)
    role = context.project_roles.get(project_id)
    if project_id not in context.projects or role is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this project.")
    project = await project_store.get(project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The project is not configured in Azure SQL.")
    return project, role[0]


def _select_resource(
    resources: tuple[AtlassianResource, ...], configured_url: str
) -> AtlassianResource:
    expected = configured_url.rstrip("/").lower()
    for resource in resources:
        if resource.url.rstrip("/").lower() == expected:
            return resource
    raise ValueError("The authorized Atlassian account cannot access the configured site.")


def _state_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _has_refresh_token(connection: ProviderConnection, secrets_store: SecretStore) -> bool:
    try:
        return bool((await secrets_store.get_json(connection.secret_reference)).get("refreshToken"))
    except (ValueError, KeyError):
        return False


async def _valid_atlassian_connection(
    connection: ProviderConnection,
    client: AtlassianOAuthClient,
    store: IntegrationStore,
    secrets_store: SecretStore,
) -> tuple[ProviderConnection, str]:
    credentials = await secrets_store.get_json(connection.secret_reference)
    access_token = str(credentials.get("accessToken") or "")
    if not connection.token_is_expired and access_token:
        return connection, access_token
    refresh_token = str(credentials.get("refreshToken") or "")
    if not refresh_token:
        raise ValueError("The Atlassian connection has expired. Reconnect the provider.")
    refreshed = await client.refresh_credentials(refresh_token)
    now = datetime.now(UTC)
    updated = ProviderConnection(
        connected_by=connection.connected_by,
        tenant_id=connection.tenant_id,
        project_id=connection.project_id,
        provider=connection.provider,
        secret_reference=await secrets_store.put_json(
            _provider_secret_name(connection.project_id, connection.provider),
            {
                "accessToken": refreshed.access_token,
                "refreshToken": refreshed.refresh_token,
                "tokenType": refreshed.token_type,
            }
        ),
        resource_id=connection.resource_id,
        resource_url=connection.resource_url,
        resource_name=connection.resource_name,
        scopes=refreshed.scopes or connection.scopes,
        token_expires_at=refreshed.expires_at,
        connected_at=connection.connected_at,
        updated_at=now,
        last_synchronized_at=connection.last_synchronized_at,
        provider_account_id=connection.provider_account_id,
        provider_display_name=connection.provider_display_name,
        provider_email=connection.provider_email,
    )
    await store.upsert_connection(updated)
    return updated, refreshed.access_token


def _source_response(source: ProjectSource) -> ProjectSourceResponse:
    return ProjectSourceResponse(
        provider=source.provider,
        source_id=source.source_id,
        source_type=source.source_type,
        title=source.title,
        reference=source.reference,
        source_url=source.source_url,
        updated_at=source.source_updated_at,
    )


def _same_email(expected: str | None, actual: str | None) -> bool:
    return bool(expected and actual and expected.strip().lower() == actual.strip().lower())


_provider_secret_name = provider_secret_name
_callback_page = callback_page

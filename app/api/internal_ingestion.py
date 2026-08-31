import re
import secrets
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.application.ports.secrets import SecretStore
from app.config import Settings, get_settings
from app.integrations.atlassian.client import AtlassianOAuthClient
from app.integrations.dependencies import get_integration_store, get_secret_store
from app.integrations.store import IntegrationStore
from app.projects.dependencies import get_project_store
from app.projects.models import ProjectDefinition
from app.projects.store import ProjectStore

router = APIRouter(prefix="/v1/internal/ingestion", tags=["internal-ingestion"])


class AtlassianGatewayResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cloud_id: str = Field(alias="cloudId")
    resource_url: str = Field(alias="resourceUrl")


class IngestionProjectResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(alias="projectId")
    display_name: str = Field(alias="displayName")
    active: bool
    jira_projects: list[dict[str, object]] = Field(alias="jiraProjects")
    confluence_spaces: list[dict[str, object]] = Field(alias="confluenceSpaces")
    github_repositories: list[dict[str, object]] = Field(alias="githubRepositories")
    vector_store: dict[str, object] = Field(alias="vectorStore")
    ingestion_schedule: dict[str, object] = Field(alias="ingestionSchedule")
    atlassian: AtlassianGatewayResponse | None = None


@router.get(
    "/projects/{project_id}",
    response_model=IngestionProjectResponse,
    response_model_by_alias=True,
)
async def ingestion_project(
    project_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
    projects: ProjectStore = Depends(get_project_store),
    integrations: IntegrationStore = Depends(get_integration_store),
) -> IngestionProjectResponse:
    _authorize(request, authorization, settings)
    project = await projects.get(project_id)
    if project is None or not project.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active project mapping not found.")
    atlassian = await _atlassian_metadata(project, integrations)
    return _project_response(project, atlassian)


@router.get(
    "/github-project",
    response_model=IngestionProjectResponse,
    response_model_by_alias=True,
)
async def ingestion_project_by_repository(
    owner: str,
    repository: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
    projects: ProjectStore = Depends(get_project_store),
) -> IngestionProjectResponse:
    _authorize(request, authorization, settings)
    resolved = await projects.find_by_github_repository(owner, repository)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository mapping not found.")
    project, mapping = resolved
    scoped = ProjectDefinition(
        project_id=project.project_id,
        display_name=project.display_name,
        active=project.active,
        jira_projects=project.jira_projects,
        confluence_spaces=project.confluence_spaces,
        github_repositories=(mapping,),
        vector_store=project.vector_store,
        ingestion_schedule=project.ingestion_schedule,
    )
    return _project_response(scoped, None)


@router.get("/projects/{project_id}/atlassian")
async def proxy_atlassian_read(
    project_id: str,
    request: Request,
    target: Annotated[str, Query(min_length=1, max_length=4096)],
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
    projects: ProjectStore = Depends(get_project_store),
    integrations: IntegrationStore = Depends(get_integration_store),
    secret_store: SecretStore = Depends(get_secret_store),
) -> Response:
    _authorize(request, authorization, settings)
    project = await projects.get(project_id)
    if project is None or not project.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active project mapping not found.")
    connection = await integrations.get_connection(project_id, "ATLASSIAN")
    if connection is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Atlassian is not connected.")
    _validate_atlassian_target(target, connection.resource_id)

    try:
        from app.api.integrations import _valid_atlassian_connection

        connection, access_token = await _valid_atlassian_connection(
            connection,
            AtlassianOAuthClient(settings),
            integrations,
            secret_store,
        )
        forwarded = [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key != "target"
        ]
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            upstream = await client.get(
                target,
                params=forwarded or None,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "*/*"},
            )
            upstream.raise_for_status()
    except httpx.HTTPStatusError as failure:
        code = failure.response.status_code
        # The upstream status is included deliberately. "Atlassian read failed"
        # alone forced a log dive to distinguish a rejected request (400, a bug in
        # the caller's parameters) from a revoked grant (403) or an outage (5xx) --
        # and the caller only ever saw 502 either way. A status code carries no
        # response content, so nothing sensitive travels with it.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN if code in (401, 403) else status.HTTP_502_BAD_GATEWAY,
            f"Atlassian read failed (upstream {code}).",
        ) from failure
    except (httpx.HTTPError, ValueError, KeyError) as failure:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Atlassian read failed ({type(failure).__name__}).",
        ) from failure

    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers={"Cache-Control": "no-store"},
    )


def _authorize(request: Request, authorization: str | None, settings: Settings) -> None:
    expected = settings.ingestion_internal_api_key
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if expected and supplied and secrets.compare_digest(supplied, expected):
        return
    client_host = request.client.host if request.client else ""
    if not settings.is_production and not expected and client_host in {
        "127.0.0.1",
        "::1",
        "localhost",
        "testclient",
    }:
        return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid ingestion service credential.")


async def _atlassian_metadata(
    project: ProjectDefinition, integrations: IntegrationStore
) -> AtlassianGatewayResponse | None:
    if not project.jira_projects and not project.confluence_spaces:
        return None
    connection = await integrations.get_connection(project.project_id, "ATLASSIAN")
    if connection is None:
        # Project routing is useful independently of provider authorization (for
        # example, GitHub webhook resolution).  Jira and Confluence reads remain
        # protected by the dedicated proxy endpoint, which returns 409 until the
        # backend-managed OAuth connection exists.
        return None
    return AtlassianGatewayResponse(
        cloud_id=connection.resource_id,
        resource_url=connection.resource_url,
    )


def _validate_atlassian_target(target: str, cloud_id: str) -> None:
    parsed = urlsplit(target)
    if parsed.scheme != "https" or parsed.hostname != "api.atlassian.com":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid Atlassian target.")
    prefixes = (
        f"/ex/jira/{cloud_id}/rest/api/3/search/jql",
        f"/ex/jira/{cloud_id}/rest/api/3/attachment/content/",
        f"/ex/confluence/{cloud_id}/wiki/rest/api/content/search",
        f"/ex/confluence/{cloud_id}/wiki/api/v2/pages",
        f"/ex/confluence/{cloud_id}/wiki/download/attachments/",
        f"/ex/confluence/{cloud_id}/download/attachments/",
    )
    # An attachment download cannot be expressed as a prefix without also
    # admitting every other Confluence content endpoint, so it is matched exactly.
    # Anchored at both ends, which is what keeps a traversal suffix out.
    patterns = (
        re.compile(
            rf"^/ex/confluence/{re.escape(cloud_id)}/wiki/rest/api/content/"
            r"\d+/child/attachment/[A-Za-z0-9_.-]+/download$"
        ),
    )
    if not any(parsed.path.startswith(prefix) for prefix in prefixes) and not any(
        pattern.fullmatch(parsed.path) for pattern in patterns
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Atlassian target is not allowed.")


def _project_response(
    project: ProjectDefinition, atlassian: AtlassianGatewayResponse | None
) -> IngestionProjectResponse:
    return IngestionProjectResponse(
        project_id=project.project_id,
        display_name=project.display_name,
        active=project.active,
        jira_projects=[
            {"siteUrl": item.site_url, "projectKey": item.project_key}
            for item in project.jira_projects
        ],
        confluence_spaces=[
            {
                "siteUrl": item.site_url,
                "spaceKey": item.space_key,
                "spaceId": item.space_id,
                "rootPageIds": list(item.root_page_ids),
            }
            for item in project.confluence_spaces
        ],
        github_repositories=[
            {
                "owner": item.owner,
                "repository": item.repository,
                "indexedBranches": list(item.indexed_branches),
                "includePaths": list(item.include_paths),
                "excludePaths": list(item.exclude_paths),
            }
            for item in project.github_repositories
        ],
        vector_store={
            "collectionName": project.vector_store.collection_name,
            "textField": project.vector_store.text_field,
            "embeddingField": project.vector_store.embedding_field,
            "embeddingModel": project.vector_store.embedding_model,
            "schemaVersion": project.vector_store.schema_version,
        },
        ingestion_schedule={
            "githubMergedPrEnabled": project.ingestion_schedule.github_merged_pr_enabled,
            "dailyEnabled": project.ingestion_schedule.daily_enabled,
            "dailyAt": project.ingestion_schedule.daily_at,
            "timezone": project.ingestion_schedule.timezone,
            "manualEnabled": project.ingestion_schedule.manual_enabled,
        },
        atlassian=atlassian,
    )

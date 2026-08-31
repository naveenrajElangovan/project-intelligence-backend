import re
from typing import Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import CurrentPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.graph import GraphProjectAccessReader
from app.projects.dependencies import get_project_store
from app.projects.models import (
    ConfluenceSpaceSource,
    GitHubRepositorySource,
    IngestionSchedule,
    JiraProjectSource,
    VectorStoreRoute,
    ProjectDefinition,
)
from app.projects.store import ProjectStore

router = APIRouter(prefix="/v1/projects", tags=["project-configuration"])


class JiraProjectConfiguration(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    site_url: str = Field(alias="siteUrl", min_length=1, max_length=500)
    project_key: str = Field(alias="projectKey", pattern=r"^[A-Z][A-Z0-9_]{1,39}$")


class ConfluenceSpaceConfiguration(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    site_url: str = Field(alias="siteUrl", min_length=1, max_length=500)
    space_key: str = Field(alias="spaceKey", min_length=1, max_length=255)
    space_id: str = Field(alias="spaceId", pattern=r"^[0-9]+$")
    root_page_ids: list[str] = Field(default_factory=list, alias="rootPageIds")


class GitHubRepositoryConfiguration(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    owner: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    indexed_branches: list[str] = Field(default_factory=lambda: ["main"], alias="indexedBranches")
    include_paths: list[str] = Field(default_factory=list, alias="includePaths")
    exclude_paths: list[str] = Field(default_factory=list, alias="excludePaths")


class VectorStoreConfiguration(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    collection_name: str = Field(
        default="project-intelligence",
        alias="collectionName",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,62}$",
    )
    text_field: str = Field(
        default="chunk_text",
        alias="textField",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
    )
    embedding_field: str = Field(
        default="embedding_text",
        alias="embeddingField",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
    )
    embedding_model: str = Field(
        default="multilingual-e5-large",
        alias="embeddingModel",
        pattern=r"^[A-Za-z0-9_.-]{1,100}$",
    )
    schema_version: str = Field(
        default="3", alias="schemaVersion", pattern=r"^[A-Za-z0-9_.-]{1,40}$"
    )


class IngestionScheduleConfiguration(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    github_merged_pr_enabled: bool = Field(default=True, alias="githubMergedPrEnabled")
    daily_enabled: bool = Field(default=True, alias="dailyEnabled")
    daily_at: str = Field(
        default="00:00",
        alias="dailyAt",
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
    )
    timezone: str = Field(default="America/Mexico_City", min_length=1, max_length=100)
    manual_enabled: bool = Field(default=True, alias="manualEnabled")


class ProjectConfigurationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    display_name: str = Field(alias="displayName", min_length=1, max_length=200)
    active: bool = True
    jira_projects: list[JiraProjectConfiguration] = Field(
        default_factory=list, alias="jiraProjects"
    )
    confluence_spaces: list[ConfluenceSpaceConfiguration] = Field(
        default_factory=list, alias="confluenceSpaces"
    )
    github_repositories: list[GitHubRepositoryConfiguration] = Field(
        default_factory=list, alias="githubRepositories"
    )
    vector_store: VectorStoreConfiguration = Field(alias="vectorStore")
    ingestion_schedule: IngestionScheduleConfiguration = Field(
        default_factory=IngestionScheduleConfiguration,
        alias="ingestionSchedule",
    )


class ProjectConfigurationResponse(ProjectConfigurationRequest):
    project_id: str = Field(alias="projectId")


@router.get(
    "/{project_id}/configuration",
    response_model=ProjectConfigurationResponse,
    response_model_by_alias=True,
)
async def get_project_configuration(
    project_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    store: ProjectStore = Depends(get_project_store),
) -> ProjectConfigurationResponse:
    await _require_project_role(project_id, principal.object_id, access_reader)
    project = await store.get(project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project is not configured in Azure SQL.")
    return _response(project)


@router.put(
    "/{project_id}/configuration",
    response_model=ProjectConfigurationResponse,
    response_model_by_alias=True,
)
async def put_project_configuration(
    project_id: str,
    body: ProjectConfigurationRequest,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    store: ProjectStore = Depends(get_project_store),
) -> ProjectConfigurationResponse:
    role = await _require_project_role(project_id, principal.object_id, access_reader)
    if role != "TECHNICAL_LEAD":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the project technical lead can change project source mappings.",
        )
    _validate_project_sources(body)
    project = ProjectDefinition(
        project_id=project_id,
        display_name=body.display_name,
        active=body.active,
        jira_projects=tuple(
            JiraProjectSource(
                site_url=item.site_url.rstrip("/"),
                project_key=item.project_key,
            )
            for item in body.jira_projects
        ),
        confluence_spaces=tuple(
            ConfluenceSpaceSource(
                site_url=item.site_url.rstrip("/"),
                space_key=item.space_key,
                space_id=item.space_id,
                root_page_ids=tuple(item.root_page_ids),
            )
            for item in body.confluence_spaces
        ),
        github_repositories=tuple(
            GitHubRepositorySource(
                owner=item.owner,
                repository=item.repository,
                indexed_branches=tuple(item.indexed_branches),
                include_paths=tuple(item.include_paths),
                exclude_paths=tuple(item.exclude_paths),
            )
            for item in body.github_repositories
        ),
        vector_store=VectorStoreRoute(
            collection_name=body.vector_store.collection_name,
            text_field=body.vector_store.text_field,
            embedding_field=body.vector_store.embedding_field,
            embedding_model=body.vector_store.embedding_model,
            schema_version=body.vector_store.schema_version,
        ),
        ingestion_schedule=IngestionSchedule(
            github_merged_pr_enabled=body.ingestion_schedule.github_merged_pr_enabled,
            daily_enabled=body.ingestion_schedule.daily_enabled,
            daily_at=body.ingestion_schedule.daily_at,
            timezone=body.ingestion_schedule.timezone,
            manual_enabled=body.ingestion_schedule.manual_enabled,
        ),
    )
    await store.upsert(project)
    return _response(project)


async def _require_project_role(
    project_id: str,
    user_id: str,
    access_reader: GraphProjectAccessReader,
) -> str:
    context = await access_reader.read(user_id)
    role = context.project_roles.get(project_id)
    if project_id not in context.projects or role is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this project.")
    return role


def _validate_project_sources(body: ProjectConfigurationRequest) -> None:
    for site in [*body.jira_projects, *body.confluence_spaces]:
        parsed = urlsplit(site.site_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Atlassian site URLs must be HTTPS origins without credentials.",
            )
        if parsed.query or parsed.fragment:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Atlassian site URLs cannot contain a query or fragment.",
            )
    sites = {
        item.site_url.rstrip("/").lower()
        for item in [*body.jira_projects, *body.confluence_spaces]
    }
    if len(sites) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The current Atlassian connector supports one site per project.",
        )
    repositories = {
        f"{item.owner}/{item.repository}".lower() for item in body.github_repositories
    }
    if len(repositories) != len(body.github_repositories):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "GitHub repository mappings must be unique within a project.",
        )
    for repository in body.github_repositories:
        if not repository.indexed_branches or any(
            not _valid_git_reference(branch) for branch in repository.indexed_branches
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Every GitHub repository requires valid indexed branch names.",
            )
        if any(not _valid_path_filter(path) for path in [*repository.include_paths, *repository.exclude_paths]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "GitHub path filters must be relative patterns without parent traversal.",
            )
    try:
        ZoneInfo(body.ingestion_schedule.timezone)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The ingestion schedule timezone is invalid.",
        ) from error


def _valid_git_reference(value: str) -> bool:
    return bool(
        value
        and len(value) <= 255
        and not value.startswith(("/", "."))
        and not value.endswith(("/", ".", ".lock"))
        and ".." not in value
        and "@{" not in value
        and not any(character in value for character in " ~^:?*[\\")
    )


def _valid_path_filter(value: str) -> bool:
    return bool(value and len(value) <= 500 and not value.startswith("/") and ".." not in value.split("/"))


def _response(project: ProjectDefinition) -> ProjectConfigurationResponse:
    return ProjectConfigurationResponse(
        project_id=project.project_id,
        display_name=project.display_name,
        active=project.active,
        jira_projects=[
            JiraProjectConfiguration(site_url=item.site_url, project_key=item.project_key)
            for item in project.jira_projects
        ],
        confluence_spaces=[
            ConfluenceSpaceConfiguration(
                site_url=item.site_url,
                space_key=item.space_key,
                space_id=item.space_id,
                root_page_ids=list(item.root_page_ids),
            )
            for item in project.confluence_spaces
        ],
        github_repositories=[
            GitHubRepositoryConfiguration(
                owner=item.owner,
                repository=item.repository,
                indexed_branches=list(item.indexed_branches),
                include_paths=list(item.include_paths),
                exclude_paths=list(item.exclude_paths),
            )
            for item in project.github_repositories
        ],
        vector_store=VectorStoreConfiguration(
            collection_name=project.vector_store.collection_name,
            text_field=project.vector_store.text_field,
            embedding_field=project.vector_store.embedding_field,
            embedding_model=project.vector_store.embedding_model,
            schema_version=project.vector_store.schema_version,
        ),
        ingestion_schedule=IngestionScheduleConfiguration(
            github_merged_pr_enabled=project.ingestion_schedule.github_merged_pr_enabled,
            daily_enabled=project.ingestion_schedule.daily_enabled,
            daily_at=project.ingestion_schedule.daily_at,
            timezone=project.ingestion_schedule.timezone,
            manual_enabled=project.ingestion_schedule.manual_enabled,
        ),
    )

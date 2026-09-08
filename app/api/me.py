import logging
from typing import Annotated, Any

from azure.core.exceptions import AzureError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from app.auth.dependencies import CurrentPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.graph import GraphProjectAccessReader
from app.config import Settings, get_settings
from app.projects.dependencies import get_project_store
from app.projects.store import ProjectStore

router = APIRouter(prefix="/v1", tags=["identity"])
logger = logging.getLogger(__name__)


class ProjectAssignment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(alias="projectId")
    display_name: str = Field(alias="displayName")
    role: str


class CurrentUserResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(alias="userId")
    employee_id: str | None = Field(alias="employeeId")
    username: str
    display_name: str = Field(alias="displayName")
    email: str | None
    departments: list[str]
    assigned_projects: list[ProjectAssignment] = Field(alias="assignedProjects")


@router.get("/me", response_model=CurrentUserResponse, response_model_by_alias=True)
async def me(
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
    settings: Settings = Depends(get_settings),
) -> CurrentUserResponse:
    access_context = await access_reader.read(principal.object_id)
    try:
        configured_projects = await project_store.list_by_ids(access_context.projects)
    except (AzureError, SQLAlchemyError) as error:
        logger.exception(
            "project_configuration_lookup_failed user_id=%s",
            principal.object_id,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Project configuration is temporarily unavailable.",
        ) from error
    graph_order = {
        project_id: index for index, project_id in enumerate(access_context.projects)
    }
    configured_projects = sorted(
        configured_projects,
        key=lambda project: (
            graph_order.get(project.project_id, len(graph_order)),
            project.project_id,
        ),
    )
    assignments = [
        ProjectAssignment(
            project_id=project.project_id,
            display_name=project.display_name,
            role=access_context.project_roles[project.project_id][0],
        )
        for project in configured_projects
        if project.project_id in access_context.project_roles
    ]
    if settings.log_authorization_details:
        configured_project_ids = {project.project_id for project in configured_projects}
        assigned_project_ids = {assignment.project_id for assignment in assignments}
        logger.info(
            "project_access_diagnostics user_id=%s graph_projects=%s graph_roles=%s "
            "configured_projects=%s final_assignments=%s missing_roles=%s "
            "missing_control_plane_projects=%s",
            principal.object_id,
            list(access_context.projects),
            dict(sorted(access_context.project_roles.items())),
            [
                {
                    "projectId": project.project_id,
                    "displayName": project.display_name,
                    "active": project.active,
                    "jiraSources": len(project.jira_projects),
                    "confluenceSources": len(project.confluence_spaces),
                    "githubSources": len(project.github_repositories),
                    "vectorStoreConfigured": bool(project.vector_store.collection_name),
                }
                for project in configured_projects
            ],
            sorted(assigned_project_ids),
            sorted(
                project_id
                for project_id in access_context.projects
                if project_id not in access_context.project_roles
            ),
            sorted(
                project_id
                for project_id in access_context.projects
                if project_id not in configured_project_ids
            ),
        )
    employee_id = principal.claims.get(settings.entra_employee_id_claim)
    departments = list(
        dict.fromkeys(
            [
                *_claim_values(principal.claims.get(settings.entra_departments_claim)),
                *(
                    department
                    for project_id in access_context.projects
                    for department in access_context.project_departments.get(project_id, ())
                ),
            ]
        )
    )
    return CurrentUserResponse(
        user_id=principal.object_id,
        employee_id=employee_id if isinstance(employee_id, str) else None,
        username=principal.username,
        display_name=principal.display_name,
        email=principal.email,
        departments=departments,
        assigned_projects=assignments,
    )


@router.get("/me/projects", response_model=list[ProjectAssignment], response_model_by_alias=True)
async def my_projects(
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
    settings: Settings = Depends(get_settings),
) -> list[ProjectAssignment]:
    response = await me(principal, access_reader, project_store, settings)
    return response.assigned_projects


def _claim_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(";") if item.strip()]
    return []

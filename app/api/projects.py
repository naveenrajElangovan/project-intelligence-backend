from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import CurrentPrincipal
from app.authorization.dependencies import get_graph_project_access_reader
from app.authorization.graph import GraphProjectAccessReader
from app.projects.dependencies import get_project_store
from app.projects.store import ProjectStore

router = APIRouter(prefix="/v1/projects", tags=["projects"])


class ProjectSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(alias="projectId")
    name: str
    role: str
    health: str
    progress: int
    current_stage: str = Field(alias="currentStage")
    release_target: str = Field(alias="releaseTarget")
    last_synchronized_at: str = Field(alias="lastSynchronizedAt")


class IntegrationStatus(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    connected: bool
    available: bool
    last_synchronized_at: str | None = Field(default=None, alias="lastSynchronizedAt")
    message: str | None = None


class Metric(BaseModel):
    label: str
    value: str
    severity: str = "NORMAL"


class ProjectDashboardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project: ProjectSummary
    status_summary: str = Field(alias="statusSummary")
    metrics: list[Metric]
    integrations: list[IntegrationStatus]
    missing_controls: list[str] = Field(alias="missingControls")


@router.get(
    "/{project_id}/dashboard",
    response_model=ProjectDashboardResponse,
    response_model_by_alias=True,
)
async def project_dashboard(
    project_id: str,
    principal: CurrentPrincipal,
    access_reader: Annotated[GraphProjectAccessReader, Depends(get_graph_project_access_reader)],
    project_store: ProjectStore = Depends(get_project_store),
) -> ProjectDashboardResponse:
    access_context = await access_reader.read(principal.object_id)
    role = access_context.project_roles.get(project_id)
    if project_id not in access_context.projects or role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this project.",
        )

    project = await project_store.get(project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The project is not configured in Project Intelligence.",
        )

    return ProjectDashboardResponse(
        project=ProjectSummary(
            project_id=project_id,
            name=project.display_name,
            role=role,
            health="UNKNOWN",
            progress=0,
            current_stage="NOT_SYNCHRONIZED",
            release_target="Not set",
            last_synchronized_at="Never",
        ),
        status_summary=(
            "Project knowledge is synchronized by the independent ingestion service. "
            "Provider accounts are not connected from this application."
        ),
        metrics=[],
        integrations=[],
        missing_controls=[
            "Project health signals are not available until real source ingestion completes.",
        ],
    )

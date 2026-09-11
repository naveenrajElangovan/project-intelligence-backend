from typing import Protocol

from app.projects.models import GitHubRepositorySource, ProjectDefinition


class ProjectStore(Protocol):
    async def list_active(self) -> tuple[ProjectDefinition, ...]: ...

    async def get(self, project_id: str) -> ProjectDefinition | None: ...

    async def list_by_ids(self, project_ids: tuple[str, ...]) -> tuple[ProjectDefinition, ...]: ...

    async def upsert(self, project: ProjectDefinition) -> None: ...

    async def find_by_github_repository(
        self, owner: str, repository: str
    ) -> tuple[ProjectDefinition, GitHubRepositorySource] | None: ...

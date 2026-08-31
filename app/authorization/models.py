from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProjectAccessContext:
    projects: tuple[str, ...]
    project_roles: dict[str, str]

    @property
    def assignments(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (project_id, self.project_roles[project_id])
            for project_id in self.projects
            if project_id in self.project_roles
        )

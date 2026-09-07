from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProjectAccessContext:
    projects: tuple[str, ...]
    project_roles: dict[str, tuple[str, ...]]
    project_departments: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Keep older adapters/tests source-compatible while the authoritative
        # Graph reader now preserves every assignment.
        object.__setattr__(
            self,
            "project_roles",
            {
                project: roles if isinstance(roles, tuple) else (str(roles),)
                for project, roles in self.project_roles.items()
            },
        )

    @property
    def assignments(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (project_id, self.project_roles[project_id][0])
            for project_id in self.projects
            if project_id in self.project_roles
        )

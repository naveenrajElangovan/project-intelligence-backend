"""Stable application policies for chat inference and authorization scopes."""


def model_profile(mode: str) -> str:
    return {
        "ASK": "budget",
        "ANALYZE": "standard",
        "DEEP_ANALYSIS": "complex",
    }[mode]


def retrieval_policies(
    project_id: str,
    user_id: str,
    roles: tuple[str, ...] | str,
    departments: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Create immutable server-side retrieval scopes for one authorized user."""

    role_values = (roles,) if isinstance(roles, str) else roles
    return (
        f"project:{project_id}",
        f"user:{user_id}",
        *(f"role:{project_id}:{role}" for role in dict.fromkeys(role_values)),
        *(f"department:{project_id}:{department}" for department in dict.fromkeys(departments)),
    )

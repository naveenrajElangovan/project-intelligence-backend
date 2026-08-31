"""Stable application policies for chat inference and authorization scopes."""


def model_profile(mode: str) -> str:
    return {
        "ASK": "budget",
        "ANALYZE": "standard",
        "DEEP_ANALYSIS": "complex",
    }[mode]


def retrieval_policies(project_id: str, user_id: str, role: str) -> tuple[str, ...]:
    """Create immutable server-side retrieval scopes for one authorized user."""

    return (
        f"project:{project_id}",
        f"user:{user_id}",
        f"role:{project_id}:{role}",
    )

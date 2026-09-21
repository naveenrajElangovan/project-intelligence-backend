from app.api.integration_contracts import IntegrationStatusResponse, ProjectSourceResponse
from app.api.integration_oauth import has_refresh_token
from app.application.ports.secrets import SecretStore
from app.config import Settings
from app.integrations.models import ProjectSource
from app.integrations.store import IntegrationStore
from app.projects.models import ProjectDefinition


def can_ask_questions(project: ProjectDefinition) -> bool:
    """Chat needs an active retrieval target; provider OAuth is ingestion-only."""

    return bool(project.active and project.vector_store.collection_name)


async def integration_statuses(
    project: ProjectDefinition,
    store: IntegrationStore,
    secrets_store: SecretStore,
    settings: Settings,
) -> list[IntegrationStatusResponse]:
    indexed = set(project.vector_store.indexed_providers)
    atlassian = await store.get_connection(project.project_id, "ATLASSIAN")
    connected = atlassian is not None and (
        not atlassian.token_is_expired or await has_refresh_token(atlassian, secrets_store)
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


def source_response(source: ProjectSource) -> ProjectSourceResponse:
    return ProjectSourceResponse(
        provider=source.provider,
        source_id=source.source_id,
        source_type=source.source_type,
        title=source.title,
        reference=source.reference,
        source_url=source.source_url,
        updated_at=source.source_updated_at,
    )

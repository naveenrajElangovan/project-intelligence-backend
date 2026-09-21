import hashlib
from datetime import UTC, datetime

from app.api.integration_helpers import provider_secret_name
from app.application.ports.secrets import SecretStore
from app.integrations.atlassian.client import AtlassianOAuthClient, AtlassianResource
from app.integrations.models import ProviderConnection
from app.integrations.store import IntegrationStore


def select_resource(
    resources: tuple[AtlassianResource, ...], configured_url: str
) -> AtlassianResource:
    expected = configured_url.rstrip("/").lower()
    for resource in resources:
        if resource.url.rstrip("/").lower() == expected:
            return resource
    raise ValueError("The authorized Atlassian account cannot access the configured site.")


def state_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def has_refresh_token(connection: ProviderConnection, secrets_store: SecretStore) -> bool:
    try:
        return bool((await secrets_store.get_json(connection.secret_reference)).get("refreshToken"))
    except (ValueError, KeyError):
        return False


async def valid_atlassian_connection(
    connection: ProviderConnection,
    client: AtlassianOAuthClient,
    store: IntegrationStore,
    secrets_store: SecretStore,
) -> tuple[ProviderConnection, str]:
    credentials = await secrets_store.get_json(connection.secret_reference)
    access_token = str(credentials.get("accessToken") or "")
    if not connection.token_is_expired and access_token:
        return connection, access_token
    refresh_token = str(credentials.get("refreshToken") or "")
    if not refresh_token:
        raise ValueError("The Atlassian connection has expired. Reconnect the provider.")
    refreshed = await client.refresh_credentials(refresh_token)
    now = datetime.now(UTC)
    updated = ProviderConnection(
        connected_by=connection.connected_by,
        tenant_id=connection.tenant_id,
        project_id=connection.project_id,
        provider=connection.provider,
        secret_reference=await secrets_store.put_json(
            provider_secret_name(connection.project_id, connection.provider),
            {
                "accessToken": refreshed.access_token,
                "refreshToken": refreshed.refresh_token,
                "tokenType": refreshed.token_type,
            },
        ),
        resource_id=connection.resource_id,
        resource_url=connection.resource_url,
        resource_name=connection.resource_name,
        scopes=refreshed.scopes or connection.scopes,
        token_expires_at=refreshed.expires_at,
        connected_at=connection.connected_at,
        updated_at=now,
        last_synchronized_at=connection.last_synchronized_at,
        provider_account_id=connection.provider_account_id,
        provider_display_name=connection.provider_display_name,
        provider_email=connection.provider_email,
    )
    await store.upsert_connection(updated)
    return updated, refreshed.access_token


def same_email(expected: str | None, actual: str | None) -> bool:
    return bool(expected and actual and expected.strip().lower() == actual.strip().lower())

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class OAuthState:
    state_hash: str
    user_id: str
    tenant_id: str
    project_id: str
    provider: str
    expires_at: datetime
    user_email: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    connected_by: str
    tenant_id: str
    project_id: str
    provider: str
    secret_reference: str
    resource_id: str
    resource_url: str
    resource_name: str
    scopes: tuple[str, ...]
    token_expires_at: datetime | None
    connected_at: datetime
    updated_at: datetime
    last_synchronized_at: datetime | None = None
    provider_account_id: str | None = None
    provider_display_name: str | None = None
    provider_email: str | None = None

    @property
    def token_is_expired(self) -> bool:
        return self.token_expires_at is not None and self.token_expires_at <= datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AtlassianCredentials:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_at: datetime | None
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectSource:
    project_id: str
    provider: str
    source_id: str
    source_type: str
    title: str
    reference: str
    source_url: str
    content: str
    metadata: dict[str, object]
    source_updated_at: datetime | None

from datetime import datetime
from typing import Protocol

from app.integrations.models import OAuthState, ProjectSource, ProviderConnection


class IntegrationStore(Protocol):
    async def save_oauth_state(self, state: OAuthState) -> None: ...

    async def consume_oauth_state(self, state_hash: str, provider: str) -> OAuthState | None: ...

    async def upsert_connection(self, connection: ProviderConnection) -> None: ...

    async def get_connection(
        self, project_id: str, provider: str
    ) -> ProviderConnection | None: ...

    async def save_synchronized_sources(
        self,
        project_id: str,
        provider: str,
        sources: tuple[ProjectSource, ...],
        synchronized_at: datetime,
    ) -> None: ...

    async def list_authorized_sources(
        self, project_id: str, provider: str | None, limit: int
    ) -> tuple[ProjectSource, ...]: ...

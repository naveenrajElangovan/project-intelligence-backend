from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.infrastructure.sql.models import (
    IntegrationConnectionRecord,
    OAuthStateRecord,
)
from app.integrations.models import OAuthState, ProjectSource, ProviderConnection


class SqlIntegrationRepository:
    """Azure SQL adapter for provider metadata. Provider tokens never enter SQL."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sessions = session_factory

    async def save_oauth_state(self, state: OAuthState) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                OAuthStateRecord(
                    state_hash=state.state_hash,
                    user_id=state.user_id,
                    tenant_id=state.tenant_id,
                    project_id=state.project_id,
                    provider=state.provider,
                    expires_at=state.expires_at,
                    user_email=state.user_email,
                )
            )

    async def consume_oauth_state(self, state_hash: str, provider: str) -> OAuthState | None:
        async with self._sessions() as session, session.begin():
            record = await session.scalar(
                select(OAuthStateRecord).where(
                    OAuthStateRecord.state_hash == state_hash,
                    OAuthStateRecord.provider == provider,
                    OAuthStateRecord.expires_at > datetime.now(UTC),
                )
            )
            if record is None:
                return None
            await session.delete(record)
            return OAuthState(
                state_hash=record.state_hash,
                user_id=record.user_id,
                tenant_id=record.tenant_id,
                project_id=record.project_id,
                provider=record.provider,
                expires_at=record.expires_at,
                user_email=record.user_email,
            )

    async def upsert_connection(self, connection: ProviderConnection) -> None:
        async with self._sessions() as session, session.begin():
            record = await session.scalar(
                select(IntegrationConnectionRecord).where(
                    IntegrationConnectionRecord.project_id == connection.project_id,
                    IntegrationConnectionRecord.provider == connection.provider,
                )
            )
            values = {
                "secret_reference": connection.secret_reference,
                "tenant_id": connection.tenant_id,
                "connected_by": connection.connected_by,
                "resource_id": connection.resource_id,
                "resource_url": connection.resource_url,
                "resource_name": connection.resource_name,
                "scopes": list(connection.scopes),
                "token_expires_at": connection.token_expires_at,
                "connected_at": connection.connected_at,
                "updated_at": connection.updated_at,
                "last_synchronized_at": connection.last_synchronized_at,
                "provider_account_id": connection.provider_account_id,
                "provider_display_name": connection.provider_display_name,
                "provider_email": connection.provider_email,
            }
            if record is None:
                session.add(
                    IntegrationConnectionRecord(
                        project_id=connection.project_id,
                        provider=connection.provider,
                        **values,
                    )
                )
            else:
                for name, value in values.items():
                    setattr(record, name, value)

    async def get_connection(
        self, project_id: str, provider: str
    ) -> ProviderConnection | None:
        async with self._sessions() as session:
            record = await session.scalar(
                select(IntegrationConnectionRecord).where(
                    IntegrationConnectionRecord.project_id == project_id,
                    IntegrationConnectionRecord.provider == provider,
                )
            )
            return _connection(record) if record else None

    async def save_synchronized_sources(
        self,
        project_id: str,
        provider: str,
        sources: tuple[ProjectSource, ...],
        synchronized_at: datetime,
    ) -> None:
        raise RuntimeError(
            "Source synchronization is owned by project-intelligence-ingestion; "
            "the backend never stores provider content."
        )

    async def list_authorized_sources(
        self, project_id: str, provider: str | None, limit: int
    ) -> tuple[ProjectSource, ...]:
        return ()


def _connection(record: IntegrationConnectionRecord) -> ProviderConnection:
    return ProviderConnection(
        connected_by=record.connected_by,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        provider=record.provider,
        secret_reference=record.secret_reference,
        resource_id=record.resource_id,
        resource_url=record.resource_url,
        resource_name=record.resource_name,
        scopes=tuple(record.scopes or []),
        token_expires_at=record.token_expires_at,
        connected_at=record.connected_at,
        updated_at=record.updated_at,
        last_synchronized_at=record.last_synchronized_at,
        provider_account_id=record.provider_account_id,
        provider_display_name=record.provider_display_name,
        provider_email=record.provider_email,
    )

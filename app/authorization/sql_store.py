"""SQL mirror of authenticated identities and Graph-authoritative project access."""
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.models import EntraPrincipal
from app.authorization.models import ProjectAccessContext
from app.infrastructure.sql.models import (
    AuthenticatedUserRecord,
    ProjectMembershipRecord,
    ProjectRecord,
)


class SqlIdentityAccessStore:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def record_authentication(self, principal: EntraPrincipal) -> None:
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            record = await session.get(AuthenticatedUserRecord, principal.object_id)
            values = {
                "tenant_id": principal.tenant_id,
                "subject": principal.subject,
                "username": principal.username,
                "display_name": principal.display_name,
                "email": principal.email,
                "last_authenticated_at": now,
            }
            if record is None:
                session.add(AuthenticatedUserRecord(object_id=principal.object_id, **values))
            else:
                for key, value in values.items():
                    setattr(record, key, value)

    async def synchronize_access(
        self, user_id: str, context: ProjectAccessContext
    ) -> None:
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            configured = set(
                await session.scalars(
                    select(ProjectRecord.project_id).where(
                        ProjectRecord.project_id.in_(context.projects)
                    )
                )
            ) if context.projects else set()
            existing = {
                record.project_id: record
                for record in await session.scalars(
                    select(ProjectMembershipRecord).where(
                        ProjectMembershipRecord.user_id == user_id
                    )
                )
            }
            for record in existing.values():
                record.active = False
                record.last_verified_at = now
            for project_id in configured:
                roles = context.project_roles.get(project_id)
                if not roles:
                    continue
                record = existing.get(project_id)
                if record is None:
                    session.add(
                        ProjectMembershipRecord(
                            user_id=user_id,
                            project_id=project_id,
                            role=roles[0],
                            access_policy_id=f"project:{project_id}",
                            authority="MICROSOFT_GRAPH",
                            active=True,
                            last_verified_at=now,
                        )
                    )
                else:
                    record.role = roles[0]
                    record.access_policy_id = f"project:{project_id}"
                    record.authority = "MICROSOFT_GRAPH"
                    record.active = True
                    record.last_verified_at = now

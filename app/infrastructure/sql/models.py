from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UtcDateTime(TypeDecorator):
    """Return timezone-aware UTC on every backend, not just on Azure SQL.

    SQLite has no timezone-aware column type, so UtcDateTime()
    round-trips to a naive value there, while Azure SQL's DATETIMEOFFSET returns
    an aware one. Any code comparing a loaded timestamp against
    datetime.now(UTC) therefore works against one backend and raises TypeError
    against the other -- which is exactly the class of bug that makes a local
    control plane look like a bad idea. Normalising inside the type keeps the
    difference out of every call site.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        # A naive value is treated as UTC rather than rejected: every writer in
        # this codebase already passes datetime.now(UTC), and failing a write
        # here would be a worse outcome than normalising it.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class ProjectRecord(Base):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    jira_projects: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False, default=list)
    confluence_spaces: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False, default=list)
    github_repositories: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False, default=list)
    vector_store: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    ingestion_schedule: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    source_access_rules: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    retrieval_profile: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class OAuthStateRecord(Base):
    __tablename__ = "oauth_states"

    state_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    user_email: Mapped[str | None] = mapped_column(String(320))


class IntegrationConnectionRecord(Base):
    __tablename__ = "integration_connections"
    __table_args__ = (UniqueConstraint("project_id", "provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    secret_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False)
    connected_by: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_url: Mapped[str] = mapped_column(String(500), nullable=False)
    resource_name: Mapped[str] = mapped_column(String(200), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    token_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    connected_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    last_synchronized_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    provider_account_id: Mapped[str | None] = mapped_column(String(200))
    provider_display_name: Mapped[str | None] = mapped_column(String(200))
    provider_email: Mapped[str | None] = mapped_column(String(320))


class AuthenticatedUserRecord(Base):
    __tablename__ = "authenticated_users"

    object_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    username: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(320), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    last_authenticated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class ProjectMembershipRecord(Base):
    __tablename__ = "project_memberships"
    __table_args__ = (UniqueConstraint("user_id", "project_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("authenticated_users.object_id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    access_policy_id: Mapped[str] = mapped_column(String(220), nullable=False)
    authority: Mapped[str] = mapped_column(String(50), nullable=False, default="MICROSOFT_GRAPH")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_verified_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

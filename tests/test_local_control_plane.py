"""A local SQLite control plane has to behave like Azure SQL where the code cares.

The one place it does not, by default, is timestamps: SQLite has no
timezone-aware column type, so DateTime(timezone=True) round-trips to a naive
value there while Azure SQL returns an aware one. Every comparison against
datetime.now(UTC) then works on one backend and raises on the other.
"""

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.sql.models import Base, OAuthStateRecord, ProjectRecord


@pytest.fixture
def sqlite_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'control-plane.db'}")

    async def prepare():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(prepare())
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def _seed(factory, expires_at: datetime) -> None:
    now = datetime.now(UTC)

    async def write():
        async with factory() as session:
            session.add(
                ProjectRecord(
                    project_id="DEMO",
                    display_name="Example Retail Platform",
                    active=True,
                    jira_projects=[],
                    confluence_spaces=[{"siteUrl": "https://x", "spaceId": "2916360"}],
                    github_repositories=[],
                    vector_store={"collectionName": "project-intelligence", "schemaVersion": "3"},
                    ingestion_schedule={"manualEnabled": True},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                OAuthStateRecord(
                    state_hash="hash",
                    user_id="user",
                    tenant_id="tenant",
                    project_id="DEMO",
                    provider="ATLASSIAN",
                    expires_at=expires_at,
                )
            )
            await session.commit()

    asyncio.run(write())


def test_timestamps_come_back_timezone_aware_on_sqlite(sqlite_factory):
    _seed(sqlite_factory, datetime.now(UTC) + timedelta(minutes=10))

    async def read():
        async with sqlite_factory() as session:
            return (await session.execute(select(ProjectRecord))).scalar_one()

    assert asyncio.run(read()).created_at.tzinfo is not None


def test_an_expiry_can_be_compared_with_an_aware_now(sqlite_factory):
    # This is the failure the type exists to prevent: a TypeError deep inside the
    # OAuth callback rather than anything that looks like a database problem.
    _seed(sqlite_factory, datetime.now(UTC) + timedelta(minutes=10))

    async def read():
        async with sqlite_factory() as session:
            return (await session.execute(select(OAuthStateRecord))).scalar_one()

    assert asyncio.run(read()).expires_at > datetime.now(UTC)


def test_a_non_utc_offset_is_normalised_not_shifted(sqlite_factory):
    india = timezone(timedelta(hours=5, minutes=30))
    _seed(sqlite_factory, datetime(2026, 9, 1, 12, 0, tzinfo=india))

    async def read():
        async with sqlite_factory() as session:
            return (await session.execute(select(OAuthStateRecord))).scalar_one()

    assert asyncio.run(read()).expires_at == datetime(2026, 9, 1, 6, 30, tzinfo=UTC)


def test_json_columns_round_trip_as_structures(sqlite_factory):
    _seed(sqlite_factory, datetime.now(UTC) + timedelta(minutes=1))

    async def read():
        async with sqlite_factory() as session:
            return (await session.execute(select(ProjectRecord))).scalar_one()

    record = asyncio.run(read())
    assert record.confluence_spaces == [{"siteUrl": "https://x", "spaceId": "2916360"}]
    assert record.vector_store["schemaVersion"] == "3"

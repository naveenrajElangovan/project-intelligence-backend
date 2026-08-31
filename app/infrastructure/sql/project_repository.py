from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.infrastructure.sql.models import ProjectRecord
from app.projects.models import (
    ConfluenceSpaceSource,
    GitHubRepositorySource,
    IngestionSchedule,
    JiraProjectSource,
    VectorStoreRoute,
    ProjectDefinition,
)


class SqlProjectRepository:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sessions = session_factory

    async def get(self, project_id: str) -> ProjectDefinition | None:
        async with self._sessions() as session:
            record = await session.get(ProjectRecord, project_id)
            return _project(record) if record and record.active else None

    async def list_by_ids(self, project_ids: tuple[str, ...]) -> tuple[ProjectDefinition, ...]:
        if not project_ids:
            return ()
        async with self._sessions() as session:
            records = (
                await session.scalars(
                    select(ProjectRecord)
                    # SQL Server BIT columns require ``= 1``; ``IS 1`` is invalid
                    # T-SQL and is what ``is_(True)`` compiles to for this driver.
                    .where(ProjectRecord.project_id.in_(project_ids), ProjectRecord.active == 1)
                    .order_by(ProjectRecord.display_name)
                )
            ).all()
            return tuple(_project(record) for record in records)

    async def upsert(self, project: ProjectDefinition) -> None:
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            record = await session.get(ProjectRecord, project.project_id)
            values = _record_values(project)
            if record is None:
                session.add(ProjectRecord(**values, created_at=now, updated_at=now))
            else:
                for name, value in values.items():
                    setattr(record, name, value)
                record.updated_at = now

    async def find_by_github_repository(
        self, owner: str, repository: str
    ) -> tuple[ProjectDefinition, GitHubRepositorySource] | None:
        expected = f"{owner}/{repository}".lower()
        async with self._sessions() as session:
            records = (
                await session.scalars(select(ProjectRecord).where(ProjectRecord.active == 1))
            ).all()
            for record in records:
                project = _project(record)
                source = project.github_repository(owner, repository)
                if source and source.full_name.lower() == expected:
                    return project, source
        return None


def _record_values(project: ProjectDefinition) -> dict[str, object]:
    return {
        "project_id": project.project_id,
        "display_name": project.display_name,
        "active": project.active,
        "jira_projects": [
            {"siteUrl": source.site_url, "projectKey": source.project_key}
            for source in project.jira_projects
        ],
        "confluence_spaces": [
            {
                "siteUrl": source.site_url,
                "spaceKey": source.space_key,
                "spaceId": source.space_id,
                "rootPageIds": list(source.root_page_ids),
            }
            for source in project.confluence_spaces
        ],
        "github_repositories": [
            {
                "owner": source.owner,
                "repository": source.repository,
                "indexedBranches": list(source.indexed_branches),
                "includePaths": list(source.include_paths),
                "excludePaths": list(source.exclude_paths),
            }
            for source in project.github_repositories
        ],
        "vector_store": {
            "collectionName": project.vector_store.collection_name,
            "textField": project.vector_store.text_field,
            "embeddingField": project.vector_store.embedding_field,
            "embeddingModel": project.vector_store.embedding_model,
            "schemaVersion": project.vector_store.schema_version,
        },
        "ingestion_schedule": {
            "githubMergedPrEnabled": project.ingestion_schedule.github_merged_pr_enabled,
            "dailyEnabled": project.ingestion_schedule.daily_enabled,
            "dailyAt": project.ingestion_schedule.daily_at,
            "timezone": project.ingestion_schedule.timezone,
            "manualEnabled": project.ingestion_schedule.manual_enabled,
        },
    }


def _project(record: ProjectRecord) -> ProjectDefinition:
    vector_store = record.vector_store or {}
    schedule = record.ingestion_schedule or {}
    return ProjectDefinition(
        project_id=record.project_id,
        display_name=record.display_name,
        active=record.active,
        jira_projects=tuple(
            JiraProjectSource(str(item["siteUrl"]), str(item["projectKey"]))
            for item in record.jira_projects or []
        ),
        confluence_spaces=tuple(
            ConfluenceSpaceSource(
                site_url=str(item["siteUrl"]),
                space_key=str(item["spaceKey"]),
                space_id=str(item["spaceId"]),
                root_page_ids=tuple(str(value) for value in item.get("rootPageIds", [])),
            )
            for item in record.confluence_spaces or []
        ),
        github_repositories=tuple(
            GitHubRepositorySource(
                owner=str(item["owner"]),
                repository=str(item["repository"]),
                indexed_branches=tuple(str(value) for value in item.get("indexedBranches", [])),
                include_paths=tuple(str(value) for value in item.get("includePaths", [])),
                exclude_paths=tuple(str(value) for value in item.get("excludePaths", [])),
            )
            for item in record.github_repositories or []
        ),
        vector_store=VectorStoreRoute(
            collection_name=str(vector_store.get("collectionName") or "project-intelligence"),
            text_field=str(vector_store.get("textField") or "chunk_text"),
            embedding_field=str(vector_store.get("embeddingField") or "embedding_text"),
            embedding_model=str(vector_store.get("embeddingModel") or "multilingual-e5-large"),
            schema_version=str(vector_store.get("schemaVersion") or "3"),
        ),
        ingestion_schedule=IngestionSchedule(
            github_merged_pr_enabled=bool(schedule.get("githubMergedPrEnabled", True)),
            daily_enabled=bool(schedule.get("dailyEnabled", True)),
            daily_at=str(schedule.get("dailyAt") or "00:00"),
            timezone=str(schedule.get("timezone") or "America/Mexico_City"),
            manual_enabled=bool(schedule.get("manualEnabled", True)),
        ),
    )

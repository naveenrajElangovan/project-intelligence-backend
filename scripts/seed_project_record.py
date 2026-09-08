"""Upsert one project record into the control plane from a JSON description.

Why this exists: the project record lives only in the control-plane database, so
switching to a local one starts with an empty projects table and every chat
request fails project lookup. Recreating it by hand through the API needs the
service running, which needs the record -- so it is seeded directly.

Idempotent: running it twice updates in place rather than failing on the primary
key, which makes it safe to re-run after editing the JSON.

    .venv/bin/python -m scripts.seed_project_record config/local-project.DEMO.json
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.infrastructure.sql.database import get_session_factory
from app.infrastructure.sql.models import ProjectRecord


REQUIRED = ("projectId", "displayName", "vectorStore")


async def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("description", type=Path)
    arguments = parser.parse_args()

    payload = json.loads(arguments.description.read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED if not payload.get(key)]
    if missing:
        raise SystemExit(f"{arguments.description} is missing: {', '.join(missing)}")

    settings = get_settings()
    if settings.is_production:
        # The control plane is the authorization boundary; seeding it from a file
        # is a development affordance, not an administrative interface.
        raise SystemExit("Refusing to seed a project record in production.")

    now = datetime.now(UTC)
    factory = get_session_factory()
    async with factory() as session:
        existing = (
            await session.execute(
                select(ProjectRecord).where(
                    ProjectRecord.project_id == str(payload["projectId"])
                )
            )
        ).scalar_one_or_none()
        record = existing or ProjectRecord(
            project_id=str(payload["projectId"]), created_at=now
        )
        record.display_name = str(payload["displayName"])
        record.active = bool(payload.get("active", True))
        record.jira_projects = payload.get("jiraProjects") or []
        record.confluence_spaces = payload.get("confluenceSpaces") or []
        record.github_repositories = payload.get("githubRepositories") or []
        record.vector_store = payload["vectorStore"]
        record.ingestion_schedule = payload.get("ingestionSchedule") or {}
        record.source_access_rules = payload.get("sourceAccessRules") or []
        record.retrieval_profile = payload.get("retrievalProfile") or {}
        record.updated_at = now
        if existing is None:
            session.add(record)
        await session.commit()

    print(
        json.dumps(
            {
                "projectId": payload["projectId"],
                "action": "updated" if existing is not None else "created",
                "collectionName": payload["vectorStore"].get("collectionName"),
                "confluenceSpaces": len(payload.get("confluenceSpaces") or []),
                "githubRepositories": len(payload.get("githubRepositories") or []),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(_run())

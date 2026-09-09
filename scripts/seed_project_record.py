"""Upsert one project record into the control plane from a JSON description.

Why this exists: the project record lives only in the control-plane database, so
switching to a local one starts with an empty projects table and every chat
request fails project lookup. Recreating it by hand through the API needs the
service running, which needs the record -- so it is seeded directly.

Idempotent: running it twice updates in place rather than failing on the primary
key, which makes it safe to re-run after editing the JSON.

    .venv/bin/python -m scripts.seed_project_record config/local-project.DEMO.json

To update only retrieval behavior on an existing project, without replacing
source mappings or access rules:

    .venv/bin/python -m scripts.seed_project_record \
        config/local-project.DEMO.json --only retrieval-profile
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
RETRIEVAL_PROFILE_FIELDS = (
    "maxChunksPerSource",
    "rerankTopN",
    "mixedSourceTopN",
    "rerankScoreThreshold",
)


def _validated_retrieval_profile(payload: dict[str, object]) -> dict[str, int | float]:
    """Return a complete, bounded retrieval profile from a project description.

    A partial update must fail before opening a transaction when the description
    omits a setting or supplies an invalid value. This prevents an apparently
    successful profile update from silently restoring global defaults.
    """

    value = payload.get("retrievalProfile")
    if not isinstance(value, dict):
        raise SystemExit("The description must contain a retrievalProfile object.")
    missing = [field for field in RETRIEVAL_PROFILE_FIELDS if field not in value]
    if missing:
        raise SystemExit(f"retrievalProfile is missing: {', '.join(missing)}")
    integers = {
        field: int(value[field])
        for field in (
            "maxChunksPerSource",
            "rerankTopN",
            "mixedSourceTopN",
        )
    }
    if any(number < 1 or number > 50 for number in integers.values()):
        raise SystemExit("retrievalProfile integer values must be between 1 and 50.")
    threshold = float(value["rerankScoreThreshold"])
    if threshold < 0 or threshold > 1:
        raise SystemExit("rerankScoreThreshold must be between 0 and 1.")
    return {**integers, "rerankScoreThreshold": threshold}


async def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("description", type=Path)
    parser.add_argument(
        "--only",
        choices=("retrieval-profile",),
        help=(
            "Update only the named field group on an existing project. "
            "All source mappings and access rules remain byte-for-byte unchanged."
        ),
    )
    arguments = parser.parse_args()

    payload = json.loads(arguments.description.read_text(encoding="utf-8"))
    required = ("projectId",) if arguments.only else REQUIRED
    missing = [key for key in required if not payload.get(key)]
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
        if arguments.only and existing is None:
            raise SystemExit(
                f"Cannot partially update missing project {payload['projectId']!r}."
            )
        if arguments.only == "retrieval-profile":
            previous_rules = list(existing.source_access_rules or [])
            existing.retrieval_profile = _validated_retrieval_profile(payload)
            existing.updated_at = now
            await session.commit()
            await session.refresh(existing)
            if list(existing.source_access_rules or []) != previous_rules:
                raise RuntimeError("Partial update changed sourceAccessRules.")
            print(
                json.dumps(
                    {
                        "projectId": payload["projectId"],
                        "action": "retrieval-profile-updated",
                        "retrievalProfile": existing.retrieval_profile,
                        "sourceAccessRulesUnchanged": len(previous_rules),
                    },
                    indent=2,
                )
            )
            return
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

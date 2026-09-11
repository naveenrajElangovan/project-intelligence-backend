"""Promote one measured Jira staging collection with a narrow compare-and-swap.

The existing collection is retained. A durable rollback record precedes the
mapping write. This operator command never changes memberships or integrations.
"""

import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import update

from app.config import get_settings
from app.evaluations.jira_quality_gate import promotion_failures
from app.evaluations.staging import StagingManifest
from app.infrastructure.sql.database import get_session_factory
from app.infrastructure.sql.models import EvaluationTargetRecord, ProjectRecord


def verified_quality(path):
    report = json.loads(path.read_text())
    failures = promotion_failures(report)
    if failures:
        raise ValueError("Quality gates failed: " + ", ".join(failures))
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("Measured artifact fingerprints are required")
    for source, expected in artifacts.items():
        if hashlib.sha256(Path(source).read_bytes()).hexdigest() != expected:
            raise ValueError("Quality evidence changed: " + source)
    return report


def save_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


async def promote(args):
    quality = verified_quality(args.quality)
    settings = get_settings()
    target_id = quality["dataset_reference"].removeprefix("staging:")
    factory = get_session_factory()
    async with factory() as session:
        target = await session.get(EvaluationTargetRecord, target_id)
        if target is None or target.project_id != quality["project_id"]:
            raise ValueError("Quality target does not belong to the project")
        manifest = StagingManifest.model_validate(target.manifest)
        if manifest.ingestion_run_id != quality["ingestion_run_id"]:
            raise ValueError("Quality evidence identifies a different ingestion run")
        project = await session.get(ProjectRecord, target.project_id)
        if project is None or not project.active:
            raise ValueError("Project is not active")
        if project.updated_at.isoformat() != quality.get("expected_project_updated_at"):
            raise ValueError("Project changed after release preflight")
        previous = dict(project.vector_store)
        version = project.updated_at
        if previous.get("collectionName") != quality["previous_collection"]:
            raise ValueError("Project route changed since evaluation")
        if (
            previous.get("embeddingModel") != manifest.embedding_model
            or previous.get("schemaVersion") != manifest.schema_version
        ):
            raise ValueError("Project embedding or schema route changed")
    async with httpx.AsyncClient(timeout=65) as client:
        response = await client.post(
            settings.rag_service_url.rstrip("/") + "/v1/internal/evaluation-snapshot",
            headers={"Authorization": "Bearer " + settings.rag_internal_api_key},
            json={"project_id": manifest.project_id, "ingestion_run_id": manifest.ingestion_run_id},
        )
        response.raise_for_status()
        snapshot = response.json()
    if any(
        snapshot.get(name) != getattr(manifest, name)
        for name in ("inventory_sha256", "contract_sha256", "embedding_model", "schema_version")
    ):
        raise ValueError("Evaluated collection changed before promotion")
    updated = {**previous, "collectionName": manifest.collection_name}
    record = {
        "status": "PREPARED",
        "project_id": manifest.project_id,
        "previous_route": previous,
        "promoted_route": updated,
        "quality_sha256": hashlib.sha256(args.quality.read_bytes()).hexdigest(),
        "prepared_at": datetime.now(UTC).isoformat(),
        "rollback": "Restore previous_route only after verifying the current route equals promoted_route.",
    }
    save_new(args.out, record)
    async with factory() as session, session.begin():
        result = await session.execute(
            update(ProjectRecord)
            .where(
                ProjectRecord.project_id == manifest.project_id,
                ProjectRecord.active.is_(True),
                ProjectRecord.updated_at == version,
            )
            .values(vector_store=updated, updated_at=datetime.now(UTC))
        )
        if result.rowcount != 1:
            raise ValueError("Project changed concurrently; promotion cancelled")
    record.update(status="PROMOTED", promoted_at=datetime.now(UTC).isoformat())
    completion = args.out.with_suffix(args.out.suffix + ".complete")
    save_new(completion, record)
    os.replace(completion, args.out)
    print(
        json.dumps(
            {
                "project_id": manifest.project_id,
                "status": "PROMOTED",
                "collection": manifest.collection_name,
                "rollback_record": str(args.out),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(promote(parser.parse_args()))

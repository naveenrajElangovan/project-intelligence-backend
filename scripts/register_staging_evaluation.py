"""Install one audited staging target using existing operator database access.

This registers evaluation input only. It never promotes a collection or grants
user access; execution still requires the actual user's Entra token and Graph
project authorization on every case.
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4

import httpx

from app.config import get_settings
from app.evaluations.staging import StagingManifest
from app.infrastructure.sql.database import get_session_factory
from app.infrastructure.sql.models import EvaluationTargetRecord
from app.projects.dependencies import get_project_store


def validate_readiness(report, audit, project_id):
    if report.get("phase") != "full-stage" or report.get("status") != "completed":
        raise ValueError("A completed full staging run is required")
    rows = [row for row in report["projects"] if row["project_id"] == project_id]
    if len(rows) != 1 or rows[0]["status"] != "indexed_evaluation_pending":
        raise ValueError("Project did not complete staging ingestion")
    expected_sources = 0
    for scope in rows[0]["scopes"]:
        source, index = scope["source_counts"], scope["index_counts"]
        expected = source.get("issues_collected", 0) + source.get("attachments_downloaded", 0)
        if (
            source.get("issues_discovered") != source.get("issues_collected")
            or index.get("failed") != 0
            or index.get("indexed", 0) + index.get("unchanged", 0) != expected
        ):
            raise ValueError("Source inventory does not reconcile with indexing")
        expected_sources += expected
    if (
        not expected_sources
        or audit.get("jira_sources") != expected_sources
        or audit.get("structural_quality_passed") is not True
        or audit.get("defects") != []
        or audit.get("security_findings") != []
        or audit.get("collection") != rows[0]["target_collection"]
    ):
        raise ValueError("Persisted chunk/security audit did not pass")
    return rows[0]


async def register(args):
    report = json.loads(args.run_report.read_text())
    audit = json.loads(args.audit.read_text())
    row = validate_readiness(report, audit, args.project)
    project = await get_project_store().get(args.project)
    if project is None or not project.active:
        raise ValueError("Project is not active")
    cases = [json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
    if any(case["project_id"] != args.project for case in cases):
        raise ValueError("Evaluation dataset contains another project")
    settings = get_settings()
    async with httpx.AsyncClient(timeout=65) as client:
        response = await client.post(
            settings.rag_service_url.rstrip("/") + "/v1/internal/evaluation-snapshot",
            headers={"Authorization": "Bearer " + settings.rag_internal_api_key},
            json={"project_id": args.project, "ingestion_run_id": report["run_id"]},
        )
        response.raise_for_status()
        snapshot = response.json()
    manifest = StagingManifest(
        project_id=args.project,
        ingestion_run_id=report["run_id"],
        inventory_sha256=snapshot["inventory_sha256"],
        contract_sha256=snapshot["contract_sha256"],
        embedding_model=snapshot["embedding_model"],
        schema_version=snapshot["schema_version"],
        cases=[
            {
                "case_id": case["id"],
                "question": case["question"],
                "language": case["query_language"],
            }
            for case in cases
        ],
    )
    if (
        manifest.collection_name != row["target_collection"]
        or manifest.embedding_model != project.vector_store.embedding_model
        or manifest.schema_version != project.vector_store.schema_version
    ):
        raise ValueError("Live staging snapshot does not match the project route")
    target_id = str(uuid4())
    async with get_session_factory()() as session, session.begin():
        session.add(
            EvaluationTargetRecord(
                target_id=target_id,
                project_id=args.project,
                manifest=manifest.model_dump(),
                expires_at=datetime.now(UTC) + timedelta(hours=4),
            )
        )
    result = {
        "datasetReference": "staging:" + target_id,
        "projectId": args.project,
        "manifest_sha256": manifest.fingerprint,
        "cases": len(cases),
        "production_modified": False,
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    for name in ("run-report", "audit", "cases", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    asyncio.run(register(parser.parse_args()))

import hashlib
import json
import pytest
from scripts.promote_jira_staging import verified_quality, save_new
from tests.test_jira_quality_gate import passing


def test_changed_evidence_blocks_an_otherwise_passing_report(tmp_path):
    evidence = tmp_path / "measured.json"
    evidence.write_text('{"measured":true}')
    report = tmp_path / "quality.json"
    report.write_text(
        json.dumps(
            {
                **passing(),
                "artifacts": {str(evidence): hashlib.sha256(evidence.read_bytes()).hexdigest()},
            }
        )
    )
    assert verified_quality(report)["full_ingestion_complete"]
    evidence.write_text('{"measured":false}')
    with pytest.raises(ValueError, match="evidence changed"):
        verified_quality(report)


def test_no_rollback_record_can_be_overwritten(tmp_path):
    record = tmp_path / "rollback.json"
    save_new(record, {"previous_route": "original"})
    with pytest.raises(FileExistsError):
        save_new(record, {"previous_route": "wrong"})
    assert json.loads(record.read_text()) == {"previous_route": "original"}


def test_promotion_changes_only_the_selected_collection_route(tmp_path, monkeypatch):
    import asyncio
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace as NS
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.infrastructure.sql.models import Base, ProjectRecord, EvaluationTargetRecord
    from app.evaluations.staging import StagingManifest
    import scripts.promote_jira_staging as operator

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        manifest = StagingManifest(
            project_id="OTHER",
            ingestion_run_id="release-1",
            inventory_sha256="a" * 64,
            contract_sha256="b" * 64,
            embedding_model="multilingual-e5-large",
            schema_version="3",
            cases=[{"case_id": "a", "question": "What is OPS-72 status?", "language": "en"}],
        )
        route = {
            "collectionName": "original",
            "embeddingModel": "multilingual-e5-large",
            "schemaVersion": "3",
        }
        async with factory() as session, session.begin():
            for project_id in ("OTHER", "UNTOUCHED"):
                session.add(
                    ProjectRecord(
                        project_id=project_id,
                        display_name=project_id,
                        active=True,
                        vector_store=route,
                        jira_projects=[{"projectKey": "OPS"}],
                        source_access_rules=[{"accessPolicyId": "private"}],
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            session.add(
                EvaluationTargetRecord(
                    target_id="target",
                    project_id="OTHER",
                    manifest=manifest.model_dump(),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        quality = {
            "dataset_reference": "staging:target",
            "project_id": "OTHER",
            "ingestion_run_id": "release-1",
            "previous_collection": "original",
        }
        async with factory() as session:
            quality["expected_project_updated_at"] = (
                await session.get(ProjectRecord, "OTHER")
            ).updated_at.isoformat()
        quality_file = tmp_path / "quality-valid.json"
        quality_file.write_text(json.dumps(quality))
        monkeypatch.setattr(operator, "verified_quality", lambda path: quality)
        monkeypatch.setattr(operator, "get_session_factory", lambda: factory)
        monkeypatch.setattr(
            operator,
            "get_settings",
            lambda: NS(rag_service_url="http://test", rag_internal_api_key="private-test-value"),
        )

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                return NS(raise_for_status=lambda: None, json=lambda: manifest.model_dump())

        monkeypatch.setattr(operator.httpx, "AsyncClient", Client)
        out = tmp_path / "promotion.json"
        await operator.promote(NS(quality=quality_file, out=out))
        async with factory() as session:
            changed = await session.get(ProjectRecord, "OTHER")
            untouched = await session.get(ProjectRecord, "UNTOUCHED")
            assert changed.vector_store["collectionName"] == "jira-stage-release-1"
            assert changed.display_name == "OTHER" and changed.source_access_rules == [
                {"accessPolicyId": "private"}
            ]
            assert changed.jira_projects == [{"projectKey": "OPS"}]
            assert untouched.vector_store == route
        assert json.loads(out.read_text())["previous_route"] == route
        await engine.dispose()

    asyncio.run(run())

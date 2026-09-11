from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.evaluations.staging import StagingManifest, execute_case
from app.projects.models import VectorStoreRoute
from app.authorization.models import ProjectAccessContext


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fixture():
    manifest = StagingManifest(
        project_id="OTHER",
        ingestion_run_id="generic-123",
        inventory_sha256="a" * 64,
        contract_sha256="b" * 64,
        embedding_model="multilingual-e5-large",
        schema_version="3",
        cases=[{"case_id": "status-en", "question": "What is OPS-72's status?", "language": "en"}],
    )
    target = NS(
        project_id="OTHER",
        manifest=manifest.model_dump(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    run = NS(
        run_id="run",
        project_id="OTHER",
        requested_by="user",
        status="READY",
        request_payload={
            "staging_target_id": "opaque",
            "staging_manifest_sha256": manifest.fingerprint,
            "candidate_configuration": {"model_profile": "standard"},
        },
    )
    access = ProjectAccessContext(("OTHER",), {"OTHER": ("DEV",)}, {"OTHER": ("OPS",)})
    project = NS(
        project_id="OTHER", active=True, vector_store=VectorStoreRoute(), retrieval_profile=None
    )
    return dict(
        principal=NS(object_id="user"),
        run=run,
        case_id="status-en",
        reader=NS(read=AsyncMock(return_value=access)),
        projects=NS(get=AsyncMock(return_value=project)),
        store=NS(
            target=AsyncMock(return_value=target),
            claim=AsyncMock(return_value=None),
            finish=AsyncMock(),
        ),
        rag=NS(
            verify_evaluation_snapshot=AsyncMock(),
            answer=AsyncMock(return_value={"answer": "Open", "sources": []}),
        ),
    )


@pytest.mark.anyio
async def test_actual_principal_policies_and_server_target_are_used():
    args = fixture()
    assert await execute_case(**args) == {"answer": "Open", "sources": []}
    call = args["rag"].answer.call_args
    assert call.args[2] == ("project:OTHER", "user:user", "role:OTHER:DEV", "department:OTHER:OPS")
    assert call.args[3] == "jira-stage-generic-123"
    assert args["reader"].read.await_count == 2
    assert args["store"].finish.call_args.args[2] == "COMPLETED"


@pytest.mark.anyio
@pytest.mark.parametrize("expired", [False, True])
async def test_sqlite_naive_utc_target_expiry(expired):
    args = fixture()
    args["store"].target.return_value.expires_at = (
        datetime.now(UTC) + timedelta(hours=-1 if expired else 1)
    ).replace(tzinfo=None)
    if expired:
        with pytest.raises(HTTPException) as failure:
            await execute_case(**args)
        assert failure.value.status_code == 409
        args["rag"].answer.assert_not_called()
    else:
        assert (await execute_case(**args))["answer"] == "Open"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure", ["owner", "revoked", "role", "expired", "project", "manifest", "schema", "case"]
)
async def test_fail_closed_before_generation(failure):
    args = fixture()
    target = args["store"].target.return_value
    if failure == "owner":
        args["principal"].object_id = "someone-else"
    if failure == "revoked":
        args["reader"].read.return_value = ProjectAccessContext((), {})
    if failure == "role":
        args["reader"].read.return_value = ProjectAccessContext(("OTHER",), {})
    if failure == "expired":
        target.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if failure == "project":
        target.project_id = "UNAUTHORIZED"
    if failure == "manifest":
        target.manifest["inventory_sha256"] = "c" * 64
    if failure == "schema":
        target.manifest["schema_version"] = "99"
    if failure == "case":
        args["case_id"] = "unknown"
    with pytest.raises(HTTPException):
        await execute_case(**args)
    args["rag"].answer.assert_not_called()
    args["store"].claim.assert_not_called()


@pytest.mark.anyio
async def test_revocation_during_generation_discards_response():
    args = fixture()
    args["reader"].read.side_effect = [
        args["reader"].read.return_value,
        ProjectAccessContext((), {}),
    ]
    with pytest.raises(HTTPException) as failure:
        await execute_case(**args)
    assert failure.value.status_code == 403
    assert args["store"].finish.call_args.args[2:] == ("FAILED", {})


@pytest.mark.anyio
async def test_duplicate_case_never_repeats_an_uncertain_generation():
    args = fixture()
    args["store"].claim.return_value = NS(policy_hash="unknown", status="RUNNING")
    with pytest.raises(HTTPException) as failure:
        await execute_case(**args)
    assert failure.value.status_code == 409
    args["rag"].answer.assert_not_called()


def test_manifest_rejects_collection_and_policy_overrides():
    from pydantic import ValidationError

    raw = fixture()["store"].target.return_value.manifest
    with pytest.raises(ValidationError):
        StagingManifest.model_validate({**raw, "collection_name": "production"})
    with pytest.raises(ValidationError):
        StagingManifest.model_validate({**raw, "access_policy_ids": ["admin"]})


@pytest.mark.anyio
async def test_sql_claim_survives_reopening_and_prevents_duplicate_execution(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.evaluations.staging import StagingStore
    from app.infrastructure.sql.models import Base, EvaluationRunRecord

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claims.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session, session.begin():
            session.add(
                EvaluationRunRecord(
                    run_id="run",
                    project_id="OTHER",
                    requested_by="user",
                    status="READY",
                    evaluator_suite_version="v1",
                    request_payload={},
                    aggregate_scores={},
                    typed_failures=[],
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        store = StagingStore(sessions)
        assert await store.claim("run", "case", "a" * 64) is None
        duplicate = await StagingStore(sessions).claim("run", "case", "a" * 64)
        assert duplicate.status == "RUNNING"
        await store.finish("run", "case", "COMPLETED", {"answer": "verified"})
        cached = await StagingStore(sessions).claim("run", "case", "a" * 64)
        assert cached.response == {"answer": "verified"}
    finally:
        await engine.dispose()

"""Bounded staging execution using the same Graph authorization as chat.

Targets are installed by an operator after collection reconciliation. There is
deliberately no public target-creation or collection/policy override interface.
"""

from datetime import UTC, datetime
import hashlib
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError

from app.application.chat_policy import retrieval_policies
from app.infrastructure.sql.models import EvaluationCaseRecord, EvaluationTargetRecord


class StagingCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=2, max_length=4000)
    language: Literal["en", "es"]


class StagingManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str = Field(min_length=1, max_length=100)
    ingestion_run_id: str = Field(pattern=r"^[a-z0-9-]{6,37}$")
    inventory_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    embedding_model: str = Field(min_length=1, max_length=200)
    schema_version: str = Field(min_length=1, max_length=40)
    cases: list[StagingCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be distinct")
        return self

    @property
    def collection_name(self) -> str:
        return "jira-stage-" + self.ingestion_run_id

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


class StagingStore:
    def __init__(self, sessions):
        self.sessions = sessions

    async def target(self, target_id: str):
        async with self.sessions() as session:
            return await session.get(EvaluationTargetRecord, target_id)

    async def claim(self, run_id: str, case_id: str, policy_hash: str):
        try:
            async with self.sessions() as session, session.begin():
                session.add(
                    EvaluationCaseRecord(
                        run_id=run_id,
                        case_id=case_id,
                        policy_hash=policy_hash,
                        status="RUNNING",
                        response={},
                        updated_at=datetime.now(UTC),
                    )
                )
            return None
        except IntegrityError:
            async with self.sessions() as session:
                existing = await session.get(EvaluationCaseRecord, (run_id, case_id))
                if existing is None:
                    raise
                return existing

    async def finish(self, run_id, case_id, state, response):
        async with self.sessions() as session, session.begin():
            record = await session.get(EvaluationCaseRecord, (run_id, case_id))
            if record is None or record.status != "RUNNING":
                raise RuntimeError("Evaluation claim no longer belongs to this execution")
            record.status, record.response = state, response
            record.updated_at = datetime.now(UTC)


async def resolve_target(store, target_id: str, project):
    record = await store.target(target_id)
    if record is None or record.project_id != project.project_id:
        raise HTTPException(404, "Evaluation target was not found.")
    # SQLite returns timezone-naive values even for DateTime(timezone=True).
    # Stored target timestamps are always UTC; retain offsets on other engines.
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise HTTPException(409, "Evaluation target has expired.")
    manifest = StagingManifest.model_validate(record.manifest)
    route = project.vector_store
    if (
        not project.active
        or manifest.project_id != project.project_id
        or manifest.collection_name == route.collection_name
        or manifest.embedding_model != route.embedding_model
        or manifest.schema_version != route.schema_version
    ):
        raise HTTPException(409, "Evaluation target no longer matches project configuration.")
    return manifest


async def execute_case(*, principal, run, case_id, reader, projects, store, rag):
    # Re-authorize before returning even a cached result: membership can change
    # between submission and execution or between two calls for the same case.
    if run.requested_by != principal.object_id:
        raise HTTPException(403, "Only the requesting user can execute this evaluation.")
    access = await reader.read(principal.object_id)
    project_id = run.project_id
    if project_id not in access.projects or project_id not in access.project_roles:
        raise HTTPException(403, "You do not have access to this project.")
    project = await projects.get(project_id)
    if project is None:
        raise HTTPException(404, "The project is not configured.")
    target_id = run.request_payload.get("staging_target_id")
    if not target_id or run.status not in {"READY", "RUNNING"}:
        raise HTTPException(409, "Run is not available for staging execution.")
    target = await resolve_target(store, target_id, project)
    if target.fingerprint != run.request_payload.get("staging_manifest_sha256"):
        raise HTTPException(409, "Evaluation target changed after submission.")
    case = next((item for item in target.cases if item.case_id == case_id), None)
    if case is None:
        raise HTTPException(404, "Evaluation case was not found.")
    policies = retrieval_policies(
        project_id,
        principal.object_id,
        access.project_roles[project_id],
        access.project_departments.get(project_id, ()),
    )
    policy_hash = hashlib.sha256(json.dumps(sorted(policies)).encode()).hexdigest()
    await rag.verify_evaluation_snapshot(target)
    existing = await store.claim(run.run_id, case_id, policy_hash)
    if existing is not None:
        if existing.policy_hash != policy_hash or existing.status != "COMPLETED":
            raise HTTPException(
                409, "Case is claimed, failed, or its access scope changed; start a new run."
            )
        return existing.response
    route = project.vector_store
    try:
        response = await rag.answer(
            project_id,
            case.question,
            policies,
            target.collection_name,
            route.text_field,
            route.embedding_field,
            route.embedding_model,
            route.schema_version,
            run.request_payload["candidate_configuration"]["model_profile"],
            request_id=f"eval-{run.run_id}-{case_id}",
            evaluation=True,
            retrieval_profile=project.retrieval_profile.as_payload()
            if project.retrieval_profile
            else None,
        )
        # Deny a result if membership was revoked while generation was running.
        fresh = await reader.read(principal.object_id)
        fresh_roles = fresh.project_roles.get(project_id)
        if (
            project_id not in fresh.projects
            or fresh_roles is None
            or set(
                retrieval_policies(
                    project_id,
                    principal.object_id,
                    fresh_roles,
                    fresh.project_departments.get(project_id, ()),
                )
            )
            != set(policies)
        ):
            raise HTTPException(403, "Project access changed during evaluation.")
        await rag.verify_evaluation_snapshot(target)
        await store.finish(run.run_id, case_id, "COMPLETED", response)
        return response
    except BaseException:
        # Unknown outcomes never trigger automatic repeated model calls.
        await store.finish(run.run_id, case_id, "FAILED", {})
        raise

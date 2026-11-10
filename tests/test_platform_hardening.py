from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bootstrap.config import Settings
from app.ports.models import HandoverSummary
from app.ports.storage import ObjectMetadata
from app.shared.db import Database
from app.shared.errors import ConflictError
from app.shared.platform.models import AICallLog, AsyncJob, TaskOutbox
from app.shared.platform.service import create_job, publish_pending_outbox, update_job
from app.shared.security.authorization.schemas import CurrentUser
from app.shared.security.identity.models import OrganizationUnit, UserAccount
from tests.security_helpers import TEST_ORG_ID


def _audio_payload(filename: str = "idempotent.m4a") -> dict[str, object]:
    return {
        "shift_date": str(date.today()),
        "shift_code": "night",
        "filename": filename,
        "size_bytes": 1024,
        "mime_type": "audio/mp4",
    }


def test_http_idempotency_suppresses_duplicates_without_replaying_private_payload(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": "audio-create-20260902-1"}
    first = client.post("/api/v1/audio-records", json=_audio_payload(), headers=headers)
    replay = client.post("/api/v1/audio-records", json=_audio_payload(), headers=headers)
    conflict = client.post(
        "/api/v1/audio-records", json=_audio_payload("different.m4a"), headers=headers
    )

    assert first.status_code == 201
    assert replay.status_code == 409
    assert replay.json()["code"] == "REQUEST_ALREADY_COMPLETED"
    assert first.json()["audio"]["id"] not in replay.text
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_activated_prompt_and_model_are_snapshotted_and_logged(
    client: TestClient, settings: Settings
) -> None:
    alias = client.put(
        "/api/v1/admin/model-aliases/handover-summary",
        json={
            "provider": "fake",
            "model_name": "fake-text-enterprise-v2",
            "enabled": True,
            "config": {"temperature": 0.2, "max_tokens": 2000},
        },
    )
    assert alias.status_code == 200, alias.text
    created_prompt = client.post(
        "/api/v1/admin/prompt-templates",
        json={
            "template_code": "handover.summary",
            "system_prompt": "企业激活提示词：仅整理输入。",
            "user_template": "交接内容：{input}",
            "output_schema": HandoverSummary.model_json_schema(),
        },
    )
    assert created_prompt.status_code == 201, created_prompt.text
    activated = client.post(
        f"/api/v1/admin/prompt-templates/{created_prompt.json()['id']}:activate",
        json={"reason": "回归测试发布"},
    )
    assert activated.status_code == 200, activated.text
    config = client.post(
        "/api/v1/admin/business-configs",
        json={
            "config_code": "asr.hotwords",
            "config_type": "hotwords",
            "payload": {"hotwords": ["西气东输", "压缩机组"], "language": "zh"},
        },
    )
    assert config.status_code == 201, config.text
    config_activation = client.post(
        f"/api/v1/admin/business-configs/{config.json()['id']}:activate",
        json={"reason": "回归测试发布"},
    )
    assert config_activation.status_code == 200, config_activation.text

    audio = client.post("/api/v1/audio-records", json=_audio_payload("configured.m4a")).json()
    audio_id = audio["audio"]["id"]
    assert (
        client.post(f"/api/v1/audio-records/{audio_id}/uploads:complete", json={}).status_code
        == 202
    )
    assert client.post(f"/api/v1/audio-records/{audio_id}:transcribe").status_code == 202
    segments = client.get(f"/api/v1/audio-records/{audio_id}/segments").json()
    assert "西气东输" in segments[0]["text"]
    summary = client.post(f"/api/v1/audio-records/{audio_id}:summarize")
    assert summary.status_code == 202, summary.text

    async def inspect_runtime() -> tuple[dict, tuple[str, str, str]]:
        database = Database(settings.database_url)
        try:
            async with database.session_factory() as session:
                job = await session.scalar(
                    select(AsyncJob).where(
                        AsyncJob.resource_id == UUID(audio_id),
                        AsyncJob.job_type == "handover_summary",
                    )
                )
                assert job is not None
                call = await session.scalar(select(AICallLog).where(AICallLog.job_id == job.id))
                assert call is not None
                return job.config_snapshot, (call.provider, call.model_name, call.status)
        finally:
            await database.dispose()

    snapshot, call = asyncio.run(inspect_runtime())
    assert snapshot["prompt"]["system_prompt"] == "企业激活提示词：仅整理输入。"
    assert snapshot["prompt"]["version"] == 1
    assert snapshot["model"] == "fake-text-enterprise-v2"
    assert call == ("fake", "fake-text-enterprise-v2", "succeeded")


def test_upload_rejects_a_digest_that_storage_does_not_confirm(client: TestClient) -> None:
    payload = _audio_payload("digest.m4a")
    payload["sha256"] = "a" * 64
    response = client.post("/api/v1/audio-records", json=payload)
    assert response.status_code == 201
    body = response.json()
    storage = client.app.state.providers.storage
    storage.objects[body["object_key"]] = ObjectMetadata(
        object_key=body["object_key"],
        size_bytes=1024,
        mime_type="audio/mp4",
        checksum="b" * 64,
    )
    completed = client.post(
        f"/api/v1/audio-records/{body['audio']['id']}/uploads:complete", json={}
    )
    assert completed.status_code == 409
    assert "checksum differs" in completed.json()["message"]


async def _exercise_outbox_and_state_machine(settings: Settings) -> tuple[str, int, bool]:
    database = Database(settings.database_url)
    await database.create_all()
    user = CurrentUser(
        user_id=TEST_ORG_ID,
        subject="platform-hardening",
        username="platform-hardening",
        display_name="Platform Hardening",
        organization_unit_id=TEST_ORG_ID,
        permissions={"handover:process"},
        organization_scope={TEST_ORG_ID},
    )
    try:
        async with database.session_factory() as session:
            session.add(
                OrganizationUnit(
                    id=TEST_ORG_ID,
                    code="TEST-ROOT",
                    name="Test Headquarters",
                    unit_type="company",
                    path="/TEST-ROOT",
                    active=True,
                )
            )
            session.add(
                UserAccount(
                    id=TEST_ORG_ID,
                    external_issuer=settings.jwt_issuer,
                    external_subject="platform-hardening",
                    username="platform-hardening",
                    display_name="Platform Hardening",
                    organization_unit_id=TEST_ORG_ID,
                    active=True,
                    authz_version=1,
                    attributes={},
                )
            )
            await session.flush()
            job = await create_job(
                session,
                user=user,
                job_type="handover_summary",
                resource_type="audio_record",
                resource_id=uuid4(),
                task_name="test.task",
                queue="test",
            )
            await session.commit()
            with pytest.raises(ConflictError):
                await update_job(session, job, status="succeeded", progress=100)

        def failing_sender(*args: object, **kwargs: object) -> None:
            raise RuntimeError("broker unavailable")

        async with database.session_factory() as session:
            await publish_pending_outbox(
                session, failing_sender, max_attempts=2, base_retry_seconds=30
            )
            row = await session.scalar(select(TaskOutbox))
            assert row is not None
            available_at = row.available_at
            if available_at.tzinfo is None:
                available_at = available_at.replace(tzinfo=UTC)
            delayed = available_at > datetime.now(UTC)
            row.available_at = datetime.now(UTC)
            await session.commit()
        async with database.session_factory() as session:
            await publish_pending_outbox(
                session, failing_sender, max_attempts=2, base_retry_seconds=30
            )
            row = await session.scalar(select(TaskOutbox))
            assert row is not None
            failed_job = await session.get(AsyncJob, row.job_id)
            assert failed_job.status == "failed"
            assert failed_job.error_code == "OUTBOX_PUBLISH_FAILED"
            from app.shared.platform.service import retry_job

            # Also exercise repair of attempts stranded queued by an older publisher.
            failed_job.status = "queued"
            await session.commit()
            retried = await retry_job(session, failed_job, user)
            assert retried.parent_job_id == failed_job.id
            assert failed_job.status == "failed"
            assert retried.status == "queued"
            await session.commit()
            # A delivery failure after execution started must not overwrite the running job.
            await update_job(session, retried, status="running", progress=1)
            await session.commit()
            await publish_pending_outbox(session, failing_sender, max_attempts=1)
            await session.refresh(retried)
            assert retried.status == "running"
            # An old pending envelope must never resurrect a terminal attempt.
            row.status = "pending"
            row.available_at = datetime.now(UTC)
            await session.commit()
            published = []
            await publish_pending_outbox(session, lambda *a, **k: published.append(a))
            assert published == []
            assert row.status == "failed"
            return row.status, row.publish_attempts, delayed
    finally:
        await database.dispose()


def test_outbox_uses_backoff_dead_letter_and_job_state_machine(settings: Settings) -> None:
    status, attempts, delayed = asyncio.run(_exercise_outbox_and_state_machine(settings))
    assert (status, attempts, delayed) == ("failed", 2, True)

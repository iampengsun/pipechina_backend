import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.bootstrap.config import Settings
from app.modules.handover.application.tasks import process_summary, process_transcription
from app.modules.handover.domain.models import AudioTranscriptVersion, HandoverSummaryVersion
from app.shared.db import Database
from app.shared.platform.models import TaskOutbox
from app.shared.platform.service import create_job, publish_pending_outbox, retry_job
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser
from app.shared.security.authorization.scopes import ScopeType
from app.shared.security.identity.models import OrganizationUnit, UserAccount
from tests.security_helpers import TEST_ORG_ID, authenticated_client


@contextmanager
def async_client(settings: Settings) -> Iterator[TestClient]:
    with authenticated_client(
        settings,
        username="admin",
        permissions={Permissions.ALL},
        scope_type=ScopeType.GLOBAL,
    ) as client:
        yield client


async def publish_and_capture(settings: Settings) -> list[dict]:
    database = Database(settings.database_url)
    captured: list[dict] = []

    def sender(name: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        captured.append({"name": name, **kwargs})

    try:
        async with database.session_factory() as session:
            await publish_pending_outbox(session, sender)
    finally:
        await database.dispose()
    return captured


async def version_counts(settings: Settings) -> tuple[int, int, int]:
    database = Database(settings.database_url)
    try:
        async with database.session_factory() as session:
            transcripts = await session.scalar(select(func.count(AudioTranscriptVersion.id)))
            summaries = await session.scalar(select(func.count(HandoverSummaryVersion.id)))
            pending = await session.scalar(
                select(func.count(TaskOutbox.id)).where(TaskOutbox.status == "pending")
            )
            return int(transcripts or 0), int(summaries or 0), int(pending or 0)
    finally:
        await database.dispose()


def test_outbox_and_worker_execute_persisted_jobs(settings: Settings) -> None:
    async_settings = settings.model_copy(update={"run_tasks_inline": False})
    with async_client(async_settings) as client:
        created = client.post(
            "/api/v1/audio-records",
            json={
                "shift_date": "2026-08-24",
                "shift_code": "night",
                "filename": "async-night.m4a",
                "size_bytes": 2048,
                "mime_type": "audio/mp4",
            },
        )
        assert created.status_code == 201, created.text
        audio_id = created.json()["audio"]["id"]
        assert (
            client.post(f"/api/v1/audio-records/{audio_id}/uploads:complete", json={}).status_code
            == 202
        )

        queued = client.post(f"/api/v1/audio-records/{audio_id}:transcribe")
        assert queued.status_code == 202, queued.text
        transcription_job_id = queued.json()["job_id"]
        duplicate = client.post(f"/api/v1/audio-records/{audio_id}:transcribe")
        assert duplicate.json()["job_id"] == transcription_job_id
        assert client.get(f"/api/v1/jobs/{transcription_job_id}").json()["status"] == "queued"

        messages = asyncio.run(publish_and_capture(async_settings))
        assert len(messages) == 1
        assert messages[0]["name"] == "app.modules.handover.transcribe_audio"
        assert messages[0]["args"] == [transcription_job_id]

        assert (
            asyncio.run(process_transcription(transcription_job_id, async_settings)) == "succeeded"
        )
        assert (
            asyncio.run(process_transcription(transcription_job_id, async_settings)) == "succeeded"
        )
        assert client.get(f"/api/v1/audio-records/{audio_id}").json()[
            "current_transcript_version_id"
        ]

        queued_summary = client.post(f"/api/v1/audio-records/{audio_id}:summarize")
        assert queued_summary.status_code == 202, queued_summary.text
        summary_job_id = queued_summary.json()["job_id"]
        assert asyncio.run(process_summary(summary_job_id, async_settings)) == "succeeded"
        assert asyncio.run(process_summary(summary_job_id, async_settings)) == "succeeded"

        transcripts, summaries, pending = asyncio.run(version_counts(async_settings))
        assert (transcripts, summaries) == (1, 1)
        assert pending == 1  # summary was executed directly without the publisher in this test


async def create_and_retry_failed_job(settings: Settings) -> tuple[str, str, int, str]:
    database = Database(settings.database_url)
    await database.create_all()
    user = CurrentUser(
        user_id=TEST_ORG_ID,
        subject="test:retry",
        username="retry-tester",
        display_name="重试测试",
        organization_unit_id=TEST_ORG_ID,
        permissions={"*"},
        organization_scope={TEST_ORG_ID},
    )
    try:
        async with database.session_factory() as session:
            session.add(
                OrganizationUnit(
                    id=TEST_ORG_ID,
                    code="TEST-ROOT",
                    name="测试总部",
                    unit_type="company",
                    path="/TEST-ROOT",
                    active=True,
                )
            )
            session.add(
                UserAccount(
                    id=TEST_ORG_ID,
                    external_issuer=settings.jwt_issuer,
                    external_subject="test:retry",
                    username="retry-tester",
                    display_name="重试测试",
                    organization_unit_id=TEST_ORG_ID,
                    active=True,
                    authz_version=1,
                    attributes={},
                )
            )
            await session.flush()
            original = await create_job(
                session,
                user=user,
                job_type="inspection_image_analysis",
                resource_type="inspection",
                resource_id=TEST_ORG_ID,
                task_name="app.modules.inspection.analyze_inspection",
                queue="vision",
                enqueue=False,
            )
            original.status = "failed"
            retried = await retry_job(session, original, user)
            await session.commit()
            outbox = await session.scalar(select(TaskOutbox).where(TaskOutbox.job_id == retried.id))
            assert outbox is not None
            return (
                str(retried.parent_job_id),
                str(original.id),
                retried.attempt,
                f"{outbox.task_name}|{outbox.queue}",
            )
    finally:
        await database.dispose()


def test_retry_creates_a_routable_child_job(settings: Settings) -> None:
    parent_id, original_id, attempt, route = asyncio.run(create_and_retry_failed_job(settings))
    assert parent_id == original_id
    assert attempt == 2
    assert route == "app.modules.inspection.analyze_inspection|vision"

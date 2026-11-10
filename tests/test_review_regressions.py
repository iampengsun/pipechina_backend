from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from app.modules.maintenance_order.application.service import transition_work_order
from app.modules.maintenance_order.domain.models import WorkOrder, WorkOrderTransition
from app.modules.meeting.application import tasks
from app.modules.meeting.domain.models import Meeting
from app.shared.errors import AppError, ConflictError
from app.shared.platform.models import AsyncJob
from app.shared.security.authorization.schemas import CurrentUser
from app.shared.security.identity.models import UserAccount
from tests.helpers import create_confirmed_event
from tests.test_meeting_flow import create_transcribed_meeting


@pytest.fixture
def meeting_worker(client, monkeypatch):
    app = client.app

    @asynccontextmanager
    async def resources(settings=None):
        yield SimpleNamespace(
            settings=app.state.settings,
            database=app.state.database,
            storage=app.state.providers.storage,
            text=app.state.providers.text,
        )

    monkeypatch.setattr(tasks, "worker_resources", resources)
    return client


@pytest.mark.parametrize("disable_requester", [False, True])
def test_minutes_worker_executes_queued_job_with_current_identity(
    meeting_worker, disable_requester
):
    client = meeting_worker
    meeting_id = create_transcribed_meeting(client)
    client.app.state.settings.run_tasks_inline = False
    queued = client.post(f"/api/v1/meetings/{meeting_id}:generate-minutes")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]

    async def disable():
        async with client.app.state.database.session_factory() as session:
            job = await session.get(AsyncJob, UUID(job_id))
            user = await session.get(UserAccount, job.requested_by)
            user.active = False
            await session.commit()

    if disable_requester:
        client.portal.call(disable)
        with pytest.raises(AppError):
            client.portal.call(tasks.process_minutes, job_id)
    else:
        assert client.portal.call(tasks.process_minutes, job_id) == "succeeded"
        minutes = client.get(f"/api/v1/meetings/{meeting_id}/minutes")
        assert minutes.status_code == 200, minutes.text
        assert minutes.json()["source"] == "ai"
        assert minutes.json()["review_status"] == "draft"


def test_retention_continues_after_protected_recordings(meeting_worker):
    client = meeting_worker
    ids = [create_transcribed_meeting(client) for _ in range(3)]
    client.app.state.settings.run_tasks_inline = False
    client.app.state.settings.meeting_recording_retention_days = 1
    for meeting_id in ids[:2]:
        queued = client.post(f"/api/v1/meetings/{meeting_id}:generate-minutes")
        assert queued.status_code == 202, queued.text

    async def expire_recordings():
        async with client.app.state.database.session_factory() as session:
            for meeting_id in ids:
                meeting = await session.get(Meeting, UUID(meeting_id))
                meeting.started_at = datetime.now(UTC) - timedelta(days=10)
                meeting.ended_at = meeting.started_at + timedelta(hours=1)
            await session.commit()

    client.portal.call(expire_recordings)
    assert client.portal.call(tasks.process_recording_retention) == 1
    statuses = [
        client.get(f"/api/v1/meetings/{meeting_id}").json()["meeting"]["upload_status"]
        for meeting_id in ids
    ]
    assert statuses == ["verified", "verified", "deleted"]


def test_work_order_rejects_stale_transition_after_another_session_commits(client):
    event_id = create_confirmed_event(client)["event_id"]
    classified = client.post(f"/api/v1/events/{event_id}:classify")
    assert classified.status_code == 202, classified.text
    workflow_id = classified.json()["workflow_id"]
    reviewed = client.post(
        f"/api/v1/workflows/{workflow_id}:review",
        json={"approved": True, "reason": "确认检查"},
    )
    assert reviewed.status_code == 200, reviewed.text
    order_id = UUID(reviewed.json()["work_order_id"])

    async def interleaved_transitions():
        factory = client.app.state.database.session_factory
        async with factory() as first, factory() as second:
            a = await first.get(WorkOrder, order_id)
            b = await second.get(WorkOrder, order_id)
            user = CurrentUser(
                user_id=a.created_by,
                subject="review",
                username="review",
                display_name="review",
                organization_unit_id=a.organization_unit_id,
                permissions={"*"},
            )
            await transition_work_order(
                first,
                order=a,
                target="pending_review",
                reason="提交审核",
                expected_version=1,
                user=user,
                permission="maintenance:edit",
            )
            with pytest.raises(ConflictError, match="work order version mismatch"):
                await transition_work_order(
                    second,
                    order=b,
                    target="cancelled",
                    reason="旧页面取消",
                    expected_version=1,
                    user=user,
                    permission="maintenance:edit",
                )
            await second.rollback()
        async with factory() as session:
            order = await session.get(WorkOrder, order_id)
            assert (order.status, order.version) == ("pending_review", 2)
            transitions = list(
                await session.scalars(
                    select(WorkOrderTransition).where(
                        WorkOrderTransition.work_order_id == order_id,
                        WorkOrderTransition.version_after == 2,
                    )
                )
            )
            assert len(transitions) == 1
            assert transitions[0].to_status == "pending_review"

    client.portal.call(interleaved_transitions)

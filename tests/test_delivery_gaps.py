"""覆盖跨模块数据正确性、会议治理和正式导出。"""

import asyncio
from datetime import date, timedelta
from functools import partial
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bootstrap.config import Settings
from app.modules.meeting.application.tasks import process_recording_retention
from app.modules.report.domain.models import OperationReport
from app.shared.db import Database
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.scopes import ScopeType
from tests.helpers import create_confirmed_event
from tests.security_helpers import authenticated_client
from tests.test_meeting_flow import create_transcribed_meeting
from tests.test_scheduled_report import prepare_database


def test_manual_source_version_must_belong_to_selected_record(client: TestClient) -> None:
    ids = []
    for content in ("一号阀门巡检正常", "二号阀门发现渗漏"):
        created = client.post(
            "/api/v1/manual-operation-records",
            json={
                "occurred_at": "2026-09-20T08:00:00+08:00",
                "record_type": "巡检记录",
                "content": content,
            },
        )
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    versions = client.get(f"/api/v1/manual-operation-records/{ids[1]}/versions").json()
    response = client.post(
        "/api/v1/event-extractions",
        json={
            "source_type": "manual_operation",
            "source_id": ids[0],
            "source_version_id": versions[0]["id"],
        },
    )
    assert response.status_code == 409, response.text


def test_attachment_requires_verified_upload(client: TestClient) -> None:
    app = cast(FastAPI, client.app)
    assert client.portal is not None
    event_id = create_confirmed_event(client)["event_id"]
    classified = client.post(f"/api/v1/events/{event_id}:classify")
    workflow_id = classified.json()["workflow_id"]
    reviewed = client.post(
        f"/api/v1/workflows/{workflow_id}:review",
        json={"approved": True, "reason": "已确认需维检"},
    )
    order_id = reviewed.json()["work_order_id"]
    created = client.post(
        f"/api/v1/work-orders/{order_id}/attachments",
        json={"filename": "现场.txt", "mime_type": "text/plain", "size_bytes": 4},
    )
    assert created.status_code == 201, created.text
    attachment_id = created.json()["attachment_id"]
    assert created.json()["status"] == "pending"
    listed = client.get(f"/api/v1/work-orders/{order_id}/attachments")
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["download_url"] is None
    # 模拟上传内容与声明长度不一致，不能确认。
    object_key = created.json()["object_key"]
    client.portal.call(
        partial(app.state.providers.storage.put_bytes, object_key, b"bad", "text/plain")
    )
    endpoint = f"/api/v1/work-orders/{order_id}/attachments/{attachment_id}/uploads:complete"
    assert client.post(endpoint, json={}).status_code == 409
    client.portal.call(
        partial(app.state.providers.storage.put_bytes, object_key, b"good", "text/plain")
    )
    verified = client.post(endpoint, json={})
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "verified"
    assert client.get(f"/api/v1/work-orders/{order_id}/attachments").json()[0]["download_url"]
    assert client.post(endpoint, json={}).status_code == 409


async def _daily_dates(settings: Settings) -> list[date]:
    database = Database(settings.database_url)
    try:
        async with database.session_factory() as session:
            return list(await session.scalars(select(OperationReport.business_date)))
    finally:
        await database.dispose()


def test_scheduled_report_uses_completed_previous_day(settings: Settings) -> None:
    from app.modules.report.application.tasks import process_daily_reports

    asyncio.run(prepare_database(settings))
    assert asyncio.run(process_daily_reports(settings)) == 1
    assert asyncio.run(_daily_dates(settings)) == [date.today() - timedelta(days=1)]


def test_pdf_export_contains_pdf_bytes(client: TestClient) -> None:
    app = cast(FastAPI, client.app)
    report = client.post(
        "/api/v1/reports",
        json={
            "report_type": "daily",
            "business_date": str(date.today()),
            "timezone": "Asia/Shanghai",
            "scope_filter": {},
        },
    )
    assert report.status_code == 201, report.text
    report_id = report.json()["id"]
    assert client.post(f"/api/v1/reports/{report_id}:generate").status_code == 202
    exported = client.post(f"/api/v1/reports/{report_id}/exports", json={"format": "pdf"})
    assert exported.status_code == 202, exported.text
    assert exported.json()["status"] == "succeeded"
    export_id = exported.json()["export_id"]
    object_key = f"reports/{report_id}/exports/{export_id}.pdf"
    assert app.state.providers.storage.contents[object_key].startswith(b"%PDF-")


def test_minutes_are_drafts_until_confirmed_and_recording_can_be_deleted(
    client: TestClient,
) -> None:
    meeting_id = create_transcribed_meeting(client)
    generated = client.post(f"/api/v1/meetings/{meeting_id}:generate-minutes")
    assert generated.status_code == 202, generated.text
    minutes = client.get(f"/api/v1/meetings/{meeting_id}/minutes")
    assert minutes.status_code == 200, minutes.text
    assert minutes.json()["source"] == "ai"
    assert minutes.json()["review_status"] == "draft"
    corrected = client.put(
        f"/api/v1/meetings/{meeting_id}/minutes",
        json={
            "content": {
                "summary": "确认阀门检查安排",
                "decisions": ["下周开展阀门检查"],
                "action_items": [{"task": "巡检阀门", "owner": "张调度"}],
                "unknowns": [],
            }
        },
    )
    assert corrected.status_code == 200, corrected.text
    confirmed = client.post(f"/api/v1/meetings/{meeting_id}/minutes:confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert (
        client.get(f"/api/v1/meetings/{meeting_id}/minutes").json()["review_status"] == "confirmed"
    )
    deleted = client.request(
        "DELETE",
        f"/api/v1/meetings/{meeting_id}/recording",
        json={"reason": "业务方授权删除录音"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["upload_status"] == "deleted"
    assert (
        client.get(f"/api/v1/meetings/{meeting_id}/minutes").json()["content"]["summary"]
        == "确认阀门检查安排"
    )


def test_knowledge_withdrawal_updates_meeting_and_allows_recording_deletion(
    client: TestClient,
) -> None:
    app = cast(FastAPI, client.app)
    app.state.settings.knowledge_backend = "fake"
    meeting_id = create_transcribed_meeting(client)
    base = client.post("/api/v1/knowledge/bases", json={"name": "会议治理库"})
    publication = client.post(
        f"/api/v1/meetings/{meeting_id}:publish-to-knowledge",
        json={"knowledge_base_id": base.json()["id"]},
    )
    assert publication.status_code == 202, publication.text
    version_id = publication.json()["knowledge_version_id"]
    blocked = client.request(
        "DELETE",
        f"/api/v1/meetings/{meeting_id}/recording",
        json={"reason": "尝试删除已关联知识的录音"},
    )
    assert blocked.status_code == 409, blocked.text
    reviewed = client.post(
        f"/api/v1/knowledge/versions/{version_id}:review",
        json={"approved": True, "reason": "逐字稿已核实"},
    )
    assert reviewed.status_code == 200, reviewed.text
    published = client.post(f"/api/v1/knowledge/versions/{version_id}:publish")
    assert published.status_code == 200, published.text
    withdrawn = client.post(
        f"/api/v1/knowledge/versions/{version_id}:withdraw",
        json={"reason": "会议资料到期撤回"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert client.get(f"/api/v1/meetings/{meeting_id}").json()["meeting"]["status"] == "withdrawn"
    deleted = client.request(
        "DELETE",
        f"/api/v1/meetings/{meeting_id}/recording",
        json={"reason": "知识已撤回，删除录音"},
    )
    assert deleted.status_code == 200, deleted.text


def test_recording_retention_is_disabled_by_default_then_deletes_due_object(
    settings: Settings, tmp_path
) -> None:
    settings.storage_provider = "local_filesystem"
    settings.local_storage_root = tmp_path / "recordings"
    with authenticated_client(
        settings,
        username="admin",
        permissions={Permissions.ALL},
        scope_type=ScopeType.GLOBAL,
    ) as client:
        app = cast(FastAPI, client.app)
        assert client.portal is not None
        created = client.post(
            "/api/v1/meetings",
            json={
                "title": "到期录音",
                "started_at": "2026-09-20T09:00:00+08:00",
                "ended_at": "2026-09-20T10:00:00+08:00",
                "filename": "meeting.m4a",
                "size_bytes": 4,
                "mime_type": "audio/mp4",
            },
        )
        assert created.status_code == 201, created.text
        meeting_id = created.json()["meeting"]["id"]
        object_key = created.json()["upload"]["object_key"]
        client.portal.call(
            partial(app.state.providers.storage.put_bytes, object_key, b"data", "audio/mp4")
        )
        assert (
            client.post(f"/api/v1/meetings/{meeting_id}/uploads:complete", json={}).status_code
            == 202
        )
        assert asyncio.run(process_recording_retention(settings)) == 0
        assert (
            client.get(f"/api/v1/meetings/{meeting_id}").json()["meeting"]["upload_status"]
            == "verified"
        )
        settings.meeting_recording_retention_days = 1
        assert asyncio.run(process_recording_retention(settings)) == 1
        assert (
            client.get(f"/api/v1/meetings/{meeting_id}").json()["meeting"]["upload_status"]
            == "deleted"
        )
        assert not (settings.local_storage_root / object_key).exists()

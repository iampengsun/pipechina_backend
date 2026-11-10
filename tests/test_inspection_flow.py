from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.helpers import create_confirmed_event


def test_inspection_image_analysis_review_and_link(client: TestClient) -> None:
    inspection = client.post(
        "/api/v1/inspections",
        json={
            "station_name": "北京输气站",
            "pipeline_name": "一号干线",
            "equipment_name": "二号阀门",
            "inspected_at": datetime.now(UTC).isoformat(),
            "notes": "例行巡检",
        },
    )
    assert inspection.status_code == 201, inspection.text
    inspection_id = inspection.json()["id"]

    image = client.post(
        f"/api/v1/inspections/{inspection_id}/images",
        json={"filename": "valve.jpg", "mime_type": "image/jpeg", "size_bytes": 2048},
    )
    assert image.status_code == 201, image.text
    image_id = image.json()["image_id"]
    completed = client.post(
        f"/api/v1/inspections/{inspection_id}/images/{image_id}:complete",
        json={},
    )
    assert completed.status_code == 200, completed.text

    analyzed = client.post(f"/api/v1/inspections/{inspection_id}:analyze")
    assert analyzed.status_code == 202, analyzed.text
    finding_id = analyzed.json()["finding_ids"][0]
    confirmed = client.post(
        f"/api/v1/findings/{finding_id}:confirm", json={"reason": "现场复核确认存在痕迹"}
    )
    assert confirmed.json()["review_status"] == "confirmed"

    event_id = create_confirmed_event(client)["event_id"]
    linked = client.post(f"/api/v1/findings/{finding_id}:link-event", json={"event_id": event_id})
    assert linked.status_code == 201, linked.text
    links = client.get(f"/api/v1/findings/{finding_id}/links").json()
    assert len(links) == 1
    assert links[0]["target_id"] == event_id

    protected = client.delete(f"/api/v1/inspections/{inspection_id}/images/{image_id}")
    assert protected.status_code == 409

    # Repeated workflow requests reuse the linked event and its current classification.
    first = client.post(f"/api/v1/findings/{finding_id}:start-workflow")
    again = client.post(f"/api/v1/findings/{finding_id}:start-workflow")
    assert first.status_code == 202, first.text
    assert again.status_code == 202, again.text
    assert first.json() == again.json()
    assert len(client.get(f"/api/v1/findings/{finding_id}/links").json()) == 2


def test_concurrent_inspection_workflow_requests_share_one_queued_job(client):
    from concurrent.futures import ThreadPoolExecutor
    from uuid import UUID

    from sqlalchemy import func, select

    from app.modules.inspection.domain.models import InspectionFinding
    from app.modules.operation_event.domain.agent_models import AgentRun

    inspection = client.post(
        "/api/v1/inspections",
        json={
            "station_name": "合成并发测试站",
            "inspected_at": datetime.now(UTC).isoformat(),
            "notes": "仅验证并发，不派发工单",
        },
    ).json()

    image = client.post(
        f"/api/v1/inspections/{inspection['id']}/images",
        json={
            "filename": "concurrency.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1024,
        },
    ).json()

    async def prepare():
        async with client.app.state.database.session_factory() as session:
            finding = InspectionFinding(
                inspection_id=UUID(inspection["id"]),
                image_id=UUID(image["image_id"]),
                title="并发测试发现",
                category="设备",
                severity="medium",
                description="合成测试观察",
                evidence="合成图片",
                confidence=0.9,
                review_status="confirmed",
            )
            session.add(finding)
            await session.commit()
            return str(finding.id)

    finding_id = client.portal.call(prepare)
    client.app.state.settings.run_tasks_inline = False
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _: client.post(f"/api/v1/findings/{finding_id}:start-workflow"), range(2)
            )
        )
    assert all(response.status_code == 202 for response in responses), [r.text for r in responses]
    assert responses[0].json() == responses[1].json()

    async def count():
        async with client.app.state.database.session_factory() as session:
            finding = await session.get(InspectionFinding, UUID(finding_id))
            return await session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.event_id == finding.workflow_event_id)
            )

    assert client.portal.call(count) == 1

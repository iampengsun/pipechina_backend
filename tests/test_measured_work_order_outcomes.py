from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.modules.maintenance_order.application.reminders import refresh_reminders
from app.modules.maintenance_order.domain.models import WorkOrder
from app.shared.security.identity.models import UserAccount
from tests.helpers import (
    create_confirmed_event,
    ensure_independent_reviewer,
    retest_measurement,
)


def executing_order(client):
    event_id = create_confirmed_event(client)["event_id"]
    workflow = client.post(f"/api/v1/events/{event_id}:classify").json()["workflow_id"]
    order_id = client.post(
        f"/api/v1/workflows/{workflow}:review",
        json={"approved": True, "reason": "合成测试：批准流程"},
    ).json()["work_order_id"]
    user = client.get("/api/v1/auth/me").json()["user_id"]
    for version, (action, extra) in enumerate(
        [
            ("submit-review", {}),
            ("review", {"approved": True}),
            ("dispatch", {"assignee_id": user}),
            ("start", {}),
        ],
        1,
    ):
        if action == "dispatch":
            invalid = client.post(
                f"/api/v1/work-orders/{order_id}:dispatch",
                json={
                    "reason": "无效负责人应被拒绝",
                    "expected_version": version,
                    "assignee_id": str(uuid4()),
                },
            )
            assert invalid.status_code == 409, invalid.text
        response = client.post(
            f"/api/v1/work-orders/{order_id}:{action}",
            json={
                "expected_version": version,
                "reason": "合成测试：推进流程",
                **extra,
            },
        )
        assert response.status_code == 200, response.text
    return order_id


def resolve(client, order, version, measurement):
    return client.post(
        f"/api/v1/work-orders/{order}:resolve",
        json={
            "expected_version": version,
            "reason": "记录合成复测",
            "action": "更换测试密封件",
            "result": "合成测试读数已记录",
            "measurements": [measurement],
        },
    )


def test_measurement_failure_rework_independent_acceptance_and_immutable_history(client):
    order = executing_order(client)
    reviewer = ensure_independent_reviewer(client)
    missing = client.post(
        f"/api/v1/work-orders/{order}:resolve",
        json={
            "expected_version": 5,
            "reason": "只有文字不足以完成整改",
        },
    )
    assert missing.status_code == 422
    failed = {**retest_measurement(), "after": 0.5}
    assert resolve(client, order, 5, failed).status_code == 200
    rows = client.get(f"/api/v1/work-orders/{order}/resolutions").json()
    acceptance = {
        "expected_version": 6,
        "reason": "独立验收合成测试",
        "resolution_id": rows[0]["id"],
        "measurements": rows[0]["measurements"],
    }
    self_review = client.post(f"/api/v1/work-orders/{order}:close", json=acceptance)
    assert self_review.status_code == 409
    failure = client.post(f"/api/v1/work-orders/{order}:close", json=acceptance, headers=reviewer)
    assert failure.status_code == 409
    relaxed = {**acceptance, "measurements": [{**failed, "maximum": 1.0}]}
    assert (
        client.post(
            f"/api/v1/work-orders/{order}:close", json=relaxed, headers=reviewer
        ).status_code
        == 409
    )
    rejected = client.post(
        f"/api/v1/work-orders/{order}:close",
        json={**acceptance, "approved": False},
        headers=reviewer,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "in_progress"
    assert resolve(client, order, 7, retest_measurement()).status_code == 200
    rows = client.get(f"/api/v1/work-orders/{order}/resolutions").json()
    assert len(rows) == 2
    assert rows[0]["acceptance"]["approved"] is False
    assert rows[0]["measurements"][0]["after"] == 0.5
    stale = {**acceptance, "expected_version": 8}
    assert (
        client.post(f"/api/v1/work-orders/{order}:close", json=stale, headers=reviewer).status_code
        == 409
    )
    accepted = client.post(
        f"/api/v1/work-orders/{order}:close",
        headers=reviewer,
        json={
            **acceptance,
            "expected_version": 8,
            "resolution_id": rows[1]["id"],
            "measurements": rows[1]["measurements"],
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "closed"
    history = client.get(f"/api/v1/work-orders/{order}/resolutions").json()
    assert history[1]["acceptance"]["approved"] is True
    assert history[1]["submitted_by"] != history[1]["acceptance"]["reviewed_by"]


def test_reminders_acknowledgement_fallback_and_terminal_resolution(client):
    order_id = executing_order(client)
    reviewer = ensure_independent_reviewer(client)

    async def generate():
        async with client.app.state.database.session_factory() as session:
            order = await session.get(WorkOrder, UUID(order_id))
            order.due_at = datetime.now(UTC) - timedelta(hours=1)
            # An assignee who is subsequently disabled must not strand the reminder.
            from sqlalchemy import select

            account = await session.scalar(
                select(UserAccount).where(UserAccount.username == "peter")
            )
            order.assignee_id = account.id
            account.active = False
            await session.commit()
            assert await refresh_reminders(session) == 1
            assert await refresh_reminders(session) == 0

    client.portal.call(generate)
    rows = client.get("/api/v1/work-order-reminders").json()
    assert len(rows) == 1
    reminder = rows[0]
    assert reminder["status"] == "pending"
    body = {"reason": "已领取逾期处理任务", "expected_version": reminder["version"]}
    ack = client.post(f"/api/v1/work-order-reminders/{reminder['id']}:acknowledge", json=body)
    assert ack.status_code == 200, ack.text
    assert ack.json()["status"] == "acknowledged"
    assert (
        client.post(
            f"/api/v1/work-order-reminders/{reminder['id']}:acknowledge", json=body
        ).status_code
        == 409
    )
    cancelled = client.post(
        f"/api/v1/work-orders/{order_id}:cancel",
        json={
            "expected_version": 5,
            "reason": "合成测试取消并结束提醒",
        },
    )
    assert cancelled.status_code == 200, cancelled.text
    rows = client.get("/api/v1/work-order-reminders?status=resolved").json()
    assert len(rows) == 1
    assert rows[0]["acknowledged_by"] is not None
    # Keep the variable to make clear that no messages are sent to the reviewer.
    assert reviewer["Authorization"]

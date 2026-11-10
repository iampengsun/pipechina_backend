from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tests.helpers import ensure_independent_reviewer, retest_measurement


def test_hazard_rectification_rejection_and_acceptance(client: TestClient) -> None:
    user_id = client.get("/api/v1/auth/me").json()["user_id"]
    created = client.post(
        "/api/v1/hazards",
        json={
            "title": "阀门填料函渗漏",
            "description": "巡检发现阀门填料函存在轻微渗漏",
            "location": "北京输气站二号阀室",
            "category": "设备设施",
            "source_type": "manual",
        },
    )
    assert created.status_code == 201, created.text
    hazard = created.json()
    assert hazard["status"] == "pending_assessment"
    assert hazard["version"] == 1

    assessed = client.post(
        f"/api/v1/hazards/{hazard['id']}:assess",
        json={
            "likelihood": 4,
            "consequence": 5,
            "rationale": "泄漏可能扩大并造成重大后果",
            "control_measures": "设置警戒并加强泄漏检测",
            "assignee_id": user_id,
            "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "expected_version": 1,
        },
    )
    assert assessed.status_code == 200, assessed.text
    hazard = assessed.json()
    assert hazard["risk_score"] == 20
    assert hazard["risk_level"] == "major"
    assert hazard["status"] == "pending_rectification"

    stale = client.post(
        f"/api/v1/hazards/{hazard['id']}:start-rectification",
        json={"reason": "开始处理", "expected_version": 1},
    )
    assert stale.status_code == 409

    started = client.post(
        f"/api/v1/hazards/{hazard['id']}:start-rectification",
        json={"reason": "现场具备整改条件", "expected_version": 2},
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "rectifying"

    submitted = client.post(
        f"/api/v1/hazards/{hazard['id']}:submit-rectification",
        json={
            "measurements": [retest_measurement()],
            "action": "紧固填料压盖并更换密封件",
            "result": "静态检查未发现泄漏",
            "expected_version": 3,
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "pending_acceptance"

    rectification = client.get(f"/api/v1/hazards/{hazard['id']}").json()["rectifications"][-1]
    reviewer = ensure_independent_reviewer(client)
    self_accept = client.post(
        f"/api/v1/hazards/{hazard['id']}:accept",
        json={
            "reason": "自己验收应拒绝",
            "expected_version": 4,
            "rectification_id": rectification["id"],
            "measurements": rectification["measurements"],
        },
    )
    assert self_accept.status_code == 409
    rejected = client.post(
        f"/api/v1/hazards/{hazard['id']}:reject",
        json={
            "reason": "还需完成带压复测",
            "expected_version": 4,
            "rectification_id": rectification["id"],
            "measurements": rectification["measurements"],
        },
        headers=reviewer,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rectifying"

    resubmitted = client.post(
        f"/api/v1/hazards/{hazard['id']}:submit-rectification",
        json={
            "measurements": [retest_measurement()],
            "action": "完成带压复测",
            "result": "连续观察两小时无泄漏",
            "expected_version": 5,
        },
    )
    assert resubmitted.status_code == 200, resubmitted.text

    rectification = client.get(f"/api/v1/hazards/{hazard['id']}").json()["rectifications"][-1]
    accepted = client.post(
        f"/api/v1/hazards/{hazard['id']}:accept",
        json={
            "reason": "现场复核合格，同意销项",
            "expected_version": 6,
            "rectification_id": rectification["id"],
            "measurements": rectification["measurements"],
        },
        headers=reviewer,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "closed"
    assert accepted.json()["closed_at"] is not None

    detail = client.get(f"/api/v1/hazards/{hazard['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert len(body["assessments"]) == 1
    assert len(body["rectifications"]) == 2
    assert [item["decision"] for item in body["acceptances"]] == ["rejected", "accepted"]
    assert len(body["transitions"]) == 6


def test_hazard_rejects_past_rectification_deadline(client: TestClient) -> None:
    user_id = client.get("/api/v1/auth/me").json()["user_id"]
    hazard = client.post(
        "/api/v1/hazards",
        json={
            "title": "围栏破损",
            "description": "站场东侧围栏破损",
            "location": "站场东侧",
            "category": "安防",
            "source_type": "manual",
        },
    ).json()
    response = client.post(
        f"/api/v1/hazards/{hazard['id']}:assess",
        json={
            "likelihood": 2,
            "consequence": 2,
            "rationale": "存在人员误入风险",
            "control_measures": "设置临时警戒",
            "assignee_id": user_id,
            "due_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "expected_version": 1,
        },
    )
    assert response.status_code == 409


def test_hazard_sources_and_assignees_must_exist(client):
    from uuid import uuid4

    body = {
        "title": "合成来源校验",
        "description": "必须能追溯来源",
        "location": "测试站场",
        "category": "设备",
        "source_type": "production_event",
    }
    assert client.post("/api/v1/hazards", json=body).status_code == 409
    assert (
        client.post("/api/v1/hazards", json={**body, "source_id": str(uuid4())}).status_code == 404
    )
    hazard = client.post("/api/v1/hazards", json={**body, "source_type": "manual"}).json()
    invalid = client.post(
        f"/api/v1/hazards/{hazard['id']}:assess",
        json={
            "expected_version": 1,
            "likelihood": 2,
            "consequence": 3,
            "rationale": "合成测试风险",
            "control_measures": "合成控制措施",
            "assignee_id": str(uuid4()),
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert invalid.status_code == 409, invalid.text


def test_hazard_attachments_are_private_until_verified(client):
    from functools import partial

    hazard = client.post(
        "/api/v1/hazards",
        json={
            "title": "附件校验",
            "description": "合成测试",
            "location": "测试站",
            "category": "设备",
            "source_type": "manual",
        },
    ).json()
    root = f"/api/v1/hazards/{hazard['id']}/attachments"
    response = client.post(
        root, json={"filename": "result.txt", "mime_type": "text/plain", "size_bytes": 4}
    )
    assert response.status_code == 201, response.text
    attachment = response.json()
    assert client.get(root).json()[0]["download_url"] is None
    client.portal.call(
        partial(
            client.app.state.providers.storage.put_bytes,
            attachment["object_key"],
            b"wrong",
            "text/plain",
        )
    )
    complete = f"{root}/{attachment['attachment_id']}/uploads:complete"
    assert client.post(complete, json={}).status_code == 409
    client.portal.call(
        partial(
            client.app.state.providers.storage.put_bytes,
            attachment["object_key"],
            b"good",
            "text/plain",
        )
    )
    assert client.post(complete, json={}).status_code == 200
    assert client.get(root).json()[0]["download_url"] is not None

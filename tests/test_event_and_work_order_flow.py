from fastapi.testclient import TestClient

from tests.helpers import create_confirmed_event


def test_candidate_event_cannot_be_classified(client: TestClient) -> None:
    extracted = client.post(
        "/api/v1/event-extractions",
        json={
            "source_type": "raw_text",
            "raw_text": "凌晨两点发现二号阀门轻微渗漏，等待人工复核。",
        },
    )
    assert extracted.status_code == 202, extracted.text
    event_id = extracted.json()["event_ids"][0]

    classified = client.post(f"/api/v1/events/{event_id}:classify")

    assert classified.status_code == 409
    assert classified.json()["code"] == "STATE_CONFLICT"


def test_event_classification_human_review_and_work_order_state_machine(
    client: TestClient,
) -> None:
    event_id = create_confirmed_event(client)["event_id"]
    classified = client.post(f"/api/v1/events/{event_id}:classify")
    assert classified.status_code == 202, classified.text
    workflow_id = classified.json()["workflow_id"]
    assert workflow_id

    workflow = client.get(f"/api/v1/workflows/{workflow_id}")
    assert workflow.json()["status"] == "awaiting_review"
    assert workflow.json()["current_node"] == "human_review"
    agent = workflow.json()["state"]["agent"]
    assert agent["kind"] == "multi_agent"
    assert agent["run_id"]
    agent_run = client.get(f"/api/v1/agent-runs/{agent['run_id']}")
    assert agent_run.status_code == 200, agent_run.text
    assert agent_run.json()["status"] == "succeeded"
    assert agent_run.json()["trace_id"]
    assert agent_run.json()["usage"]["total_tokens"] > 0
    assert [item["role"] for item in agent_run.json()["tasks"]] == [
        "coordinator",
        "evidence",
        "risk",
        "plan",
        "critic",
    ]
    evidence = client.get(f"/api/v1/agent-runs/{agent['run_id']}/evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()
    assert agent["evidence_ids"]
    quality = client.get(f"/api/v1/agent-runs/{agent['run_id']}/quality")
    assert quality.status_code == 200, quality.text
    assert quality.json()["overall_score"] == 100
    trace = client.get(f"/api/v1/agent-runs/{agent['run_id']}/trace")
    assert trace.status_code == 200, trace.text
    assert trace.json()["steps"]
    assert trace.json()["model_calls"]

    reviewed = client.post(
        f"/api/v1/workflows/{workflow_id}:review",
        json={"approved": True, "reason": "现场信息已复核，同意创建维检工单"},
    )
    assert reviewed.status_code == 200, reviewed.text
    order_id = reviewed.json()["work_order_id"]
    order = client.get(f"/api/v1/work-orders/{order_id}").json()
    assert order["status"] == "draft"
    assert order["version"] == 1

    response = client.post(
        f"/api/v1/work-orders/{order_id}:start",
        json={"reason": "非法跳过审批", "expected_version": 1},
    )
    assert response.status_code == 409

    transitions = [
        ("submit-review", {"reason": "提交值班长审批", "expected_version": 1}),
        (
            "review",
            {"approved": True, "reason": "风险与措施匹配", "expected_version": 2},
        ),
        (
            "dispatch",
            {
                "reason": "派发维检班组",
                "expected_version": 3,
                "assignee_id": client.get("/api/v1/auth/me").json()["user_id"],
            },
        ),
        ("start", {"reason": "现场开始处置", "expected_version": 4}),
        ("resolve", {"reason": "渗漏已消除并复测", "expected_version": 5}),
        ("close", {"reason": "验收通过并关闭", "expected_version": 6}),
    ]
    for action, payload in transitions:
        headers = {}
        if action == "resolve":
            from tests.helpers import retest_measurement

            payload.update(
                action="更换密封件", result="复测无泄漏", measurements=[retest_measurement()]
            )
        if action == "close":
            from tests.helpers import ensure_independent_reviewer

            rows = client.get(f"/api/v1/work-orders/{order_id}/resolutions").json()
            payload.update(resolution_id=rows[-1]["id"], measurements=rows[-1]["measurements"])
            headers = ensure_independent_reviewer(client)
        response = client.post(
            f"/api/v1/work-orders/{order_id}:{action}", json=payload, headers=headers
        )
        assert response.status_code == 200, response.text

    closed = client.get(f"/api/v1/work-orders/{order_id}").json()
    assert closed["status"] == "closed"
    assert closed["version"] == 7
    timeline = client.get(f"/api/v1/work-orders/{order_id}/timeline").json()
    assert len(timeline) == 7
    assert timeline[-1]["to_status"] == "closed"

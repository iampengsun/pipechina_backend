import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.infrastructure.providers.fake import FakeTextLLMProvider
from tests.helpers import create_confirmed_event


def closed_order(client):
    event_id = create_confirmed_event(client)["event_id"]
    classified = client.post(f"/api/v1/events/{event_id}:classify")
    assert classified.status_code == 202, classified.text
    workflow_id = classified.json()["workflow_id"]
    approved = client.post(
        f"/api/v1/workflows/{workflow_id}:review",
        json={"approved": True, "reason": "合成测试事实已核验"},
    )
    assert approved.status_code == 200, approved.text
    order_id = approved.json()["work_order_id"]
    for version, action in enumerate(
        ["submit-review", "review", "dispatch", "start", "resolve", "close"], start=1
    ):
        payload = {"expected_version": version, "reason": "合成测试：处置后复测通过"}
        if action == "review":
            payload["approved"] = True
        if action == "dispatch":
            payload["assignee_id"] = client.get("/api/v1/auth/me").json()["user_id"]
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
        result = client.post(
            f"/api/v1/work-orders/{order_id}:{action}", json=payload, headers=headers
        )
        assert result.status_code == 200, result.text
    return event_id, order_id


def consolidate(client, order_id):
    response = client.post(
        f"/api/v1/work-orders/{order_id}:consolidate-memory", json={"expected_version": 7}
    )
    assert response.status_code == 202, response.text
    rows = client.get("/api/v1/experiences?status=pending").json()
    return next(row for row in rows if row["job_id"] == response.json()["job_id"])


def reviewer(client, settings):
    from tests.helpers import ensure_independent_reviewer

    return ensure_independent_reviewer(client)


def approve(client, row, headers):
    response = client.post(
        f"/api/v1/experiences/{row['id']}:review",
        json={
            "expected_version": row["version"],
            "approved": True,
            "reason": "核对关闭记录和适用条件",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_cross_shift_observations_invalidate_old_proposals(client):
    event_id = create_confirmed_event(client)["event_id"]
    event = client.get(f"/api/v1/events/{event_id}").json()
    first = client.post(f"/api/v1/events/{event_id}:classify").json()
    note = client.post(
        f"/api/v1/events/{event_id}/observations",
        json={
            "expected_version_id": event["current_version_id"],
            "content": "交接班复测：压力稳定，仍需确认密封状态",
        },
    )
    assert note.status_code == 201, note.text
    context_url = f"/api/v1/events/{event_id}/memory"
    context = client.get(context_url).json()
    assert context["observations"][0]["status"] == "pending"
    assert context["summaries"][0]["stale"] is False
    note_id = note.json()["id"]
    reviewed = client.post(
        f"/api/v1/events/{event_id}/observations/{note_id}:review",
        json={"expected_version": 1, "action": "confirm", "reason": "值班人员核对记录"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert client.get(context_url).json()["summaries"][0]["stale"] is True
    assert (
        client.post(
            f"/api/v1/workflows/{first['workflow_id']}:review",
            json={"approved": True, "reason": "旧结论不能直接批准"},
        ).status_code
        == 409
    )
    second = client.post(f"/api/v1/events/{event_id}:classify")
    assert second.status_code == 202, second.text
    workflow = client.get(f"/api/v1/workflows/{second.json()['workflow_id']}").json()
    run_id = workflow["state"]["agent"]["run_id"]
    evidence = client.get(f"/api/v1/agent-runs/{run_id}/evidence").json()
    assert any(row["source_server"] == "case-observation" for row in evidence)
    assert (
        client.post(
            f"/api/v1/events/{event_id}/observations/{note_id}:review",
            json={"expected_version": 1, "action": "retract", "reason": "旧版本操作"},
        ).status_code
        == 409
    )
    retracted = client.post(
        f"/api/v1/events/{event_id}/observations/{note_id}:review",
        json={"expected_version": 2, "action": "retract", "reason": "仪表校验后撤回这条观察"},
    )
    assert retracted.status_code == 200, retracted.text
    assert client.get(f"/api/v1/agent-runs/{run_id}").status_code in {403, 409}


def test_closed_experience_requires_review_and_applicability_then_revokes(
    client, settings, monkeypatch
):
    _, order_id = closed_order(client)
    draft = consolidate(client, order_id)
    assert draft["checks"]["critic"]["supported"]
    assert (
        client.post(
            f"/api/v1/experiences/{draft['id']}:review",
            json={"expected_version": 1, "approved": True, "reason": "试图自行批准"},
        ).status_code
        == 409
    )
    headers = reviewer(client, settings)
    approved = approve(client, draft, headers)
    original = FakeTextLLMProvider.generate_structured
    calls = []

    async def generate(self, *, operation, system_prompt, user_prompt, response_model):
        calls.append(operation)
        if operation == "multi_agent_evidence":
            payload = json.loads(user_prompt)
            # Search once, then let the evidence report use the retrieved, reviewed experience.
            action = (
                "search_experiences"
                if payload["remaining_steps"] == payload["step_budget"]
                else "finish"
            )
            return response_model.model_validate({"action": action, "query": "", "unknowns": []})
        return await original(
            self,
            operation=operation,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
        )

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    event_id = create_confirmed_event(client)["event_id"]
    classified = client.post(f"/api/v1/events/{event_id}:classify")
    assert classified.status_code == 202, classified.text
    workflow = client.get(f"/api/v1/workflows/{classified.json()['workflow_id']}").json()
    run_id = workflow["state"]["agent"]["run_id"]
    run = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert "experience" in [task["role"] for task in run["tasks"]]
    assert "multi_agent_experience" in calls
    assert any(
        row["source_server"] == "experience-memory"
        for row in client.get(f"/api/v1/agent-runs/{run_id}/evidence").json()
    )
    withdrawn = client.post(
        f"/api/v1/experiences/{approved['id']}:withdraw",
        json={"expected_version": approved["version"], "reason": "后续证据推翻原适用条件"},
        headers=headers,
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert client.get(f"/api/v1/agent-runs/{run_id}").status_code in {403, 409}
    assert client.post(
        f"/api/v1/workflows/{classified.json()['workflow_id']}:review",
        json={"approved": True, "reason": "引用撤回经验的建议不能批准"},
    ).status_code in {403, 409}


def test_experience_revision_duplicate_resolution_merge_and_expiry(client, settings):
    _, order_id = closed_order(client)
    first = consolidate(client, order_id)
    headers = reviewer(client, settings)
    first = approve(client, first, headers)
    content = {**first["content"], "outcome": "人工核对：复测结果与工单关闭记录一致"}
    revised = client.post(
        f"/api/v1/experiences/{first['id']}:revise",
        json={
            "expected_version": first["version"],
            "content": content,
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "reason": "纠正结论措辞",
        },
    )
    assert revised.status_code == 201, revised.text
    assert client.get(f"/api/v1/experiences/{first['id']}").json()["status"] == "approved"
    second = approve(client, revised.json(), headers)
    assert client.get(f"/api/v1/experiences/{first['id']}").json()["status"] == "superseded"
    # A second closed case yields an exact duplicate/same-scope conflict. Approval must resolve it.
    _, another_order = closed_order(client)
    duplicate = consolidate(client, another_order)
    refusal = client.post(
        f"/api/v1/experiences/{duplicate['id']}:review",
        json={"expected_version": 1, "approved": True, "reason": "未处理现存冲突"},
        headers=headers,
    )
    assert refusal.status_code == 409, refusal.text
    merged = client.post(
        "/api/v1/experiences:merge",
        json={
            "expected_versions": {second["id"]: second["version"], duplicate["id"]: 1},
            "reason": "对照两次工单合并适用条件与措施",
        },
    )
    assert merged.status_code == 202, merged.text
    rows = client.get("/api/v1/experiences?status=pending").json()
    merged_row = next(row for row in rows if row["job_id"] == merged.json()["job_id"])
    merged_row = approve(client, merged_row, headers)
    assert len(merged_row["source_orders"]) == 2
    assert client.get(f"/api/v1/experiences/{second['id']}").json()["status"] == "superseded"

    async def expire_and_search():
        from app.modules.operation_event.application.memory import event_access, search_experiences
        from app.modules.operation_event.domain.memory_models import ExperienceMemory
        from app.shared.platform.models import AsyncJob
        from app.shared.worker_runtime import job_user

        async with client.app.state.database.session_factory() as session:
            row = await session.get(ExperienceMemory, UUID(merged_row["id"]))
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            job = await session.get(AsyncJob, row.job_id)
            user = await job_user(session, job)
            event = await event_access(session, user, UUID(closed_order_event))
            return await search_experiences(session, user, event, "")

    closed_order_event = client.get(f"/api/v1/work-orders/{order_id}").json()["event_id"]
    assert client.portal.call(expire_and_search) == []

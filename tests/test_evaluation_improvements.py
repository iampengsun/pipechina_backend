"""Acceptance requires a comparable new evaluation after a recorded implementation."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.modules.quality.application.service import compare_snapshots
from app.shared.errors import ConflictError
from tests.helpers import create_confirmed_event


def independent_reviewer(client, settings):
    from functools import partial

    from app.shared.security.authorization.permissions import Permissions
    from app.shared.security.authorization.scopes import ScopeType
    from tests.security_helpers import provision_test_identity, request_mock_token

    account = client.portal.call(
        partial(
            provision_test_identity,
            client.app,
            settings,
            username="peter",
            permissions={Permissions.ALL},
            scope_type=ScopeType.GLOBAL,
        )
    )
    return str(account.id), {
        "Authorization": "Bearer " + request_mock_token(settings, username="peter")
    }


def agent_evaluation(client, event_id):
    response = client.post(f"/api/v1/events/{event_id}:classify")
    assert response.status_code == 202, response.text
    run_id = response.json()["agent_run_id"]
    return client.post(
        "/api/v1/agent-evaluations",
        json={
            "name": "改进验证",
            "split": "development",
            "cases": [
                {
                    "run_id": run_id,
                    "expected_category": "equipment_leak",
                    "expected_risk_level": "medium",
                    "relevant_evidence_ids": [],
                    "action_acceptable": True,
                    "unsafe_action": False,
                }
            ],
        },
    )


def test_agent_improvement_acceptance_and_rejection_history(client: TestClient, settings):
    reviewer_id, reviewer_headers = independent_reviewer(client, settings)
    event_id = create_confirmed_event(client)["event_id"]
    baseline = agent_evaluation(client, event_id)
    assert baseline.status_code == 201, baseline.text
    response = client.post(
        "/api/v1/evaluation-improvements",
        json={
            "kind": "agent",
            "reviewer_id": reviewer_id,
            "baseline_id": baseline.json()["id"],
            "title": "优化证据引用",
            "problem": "部分引用不足",
            "measures": "补充取证提示和复核规则",
            "targets": {"category_accuracy": {"operator": "gte", "value": 1}},
        },
    )
    assert response.status_code == 201, response.text
    item = response.json()
    url = f"/api/v1/evaluation-improvements/{item['id']}"
    assert (
        client.post(
            url + ":accept",
            headers=reviewer_headers,
            json={
                "expected_version": 1,
                "approved": True,
                "reason": "提前验收",
            },
        ).status_code
        == 409
    )
    changed = client.post(
        url + ":implement",
        json={
            "expected_version": 1,
            "changes": "补充规则与回归样本",
            "revision": "git:example-v2",
        },
    )
    assert changed.status_code == 200, changed.text
    assert (
        client.post(
            url + ":implement",
            json={
                "expected_version": 1,
                "changes": "并发旧请求",
                "revision": "git:stale",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            url + ":retest",
            json={
                "expected_version": 2,
                "evaluation_id": baseline.json()["id"],
            },
        ).status_code
        == 409
    )
    old_dataset = client.get(f"/api/v1/agent-evaluations/{baseline.json()['id']}").json()["dataset"]
    rescored = client.post("/api/v1/agent-evaluations", json=old_dataset)
    assert rescored.status_code == 201, rescored.text
    assert (
        client.post(
            url + ":retest",
            json={
                "expected_version": 2,
                "evaluation_id": rescored.json()["id"],
            },
        ).status_code
        == 409
    )  # A new score for an old run is not a new investigation.
    retest = agent_evaluation(client, event_id)
    assert retest.status_code == 201, retest.text
    compared = client.post(
        url + ":retest",
        json={
            "expected_version": 2,
            "evaluation_id": retest.json()["id"],
        },
    )
    assert compared.status_code == 200, compared.text
    comparison = compared.json()["history"][-1]["comparison"]
    assert comparison["passed"] is True
    assert comparison["metrics"]["category_accuracy"] == {"before": 1, "after": 1, "delta": 0}
    rejected = client.post(
        url + ":accept",
        headers=reviewer_headers,
        json={
            "expected_version": 3,
            "approved": False,
            "reason": "继续补充人工检查",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "confirmed"
    client.post(
        url + ":implement",
        json={
            "expected_version": 4,
            "changes": "完成人工检查",
            "revision": "git:example-v3",
        },
    ).raise_for_status()
    second = agent_evaluation(client, event_id)
    client.post(
        url + ":retest",
        json={
            "expected_version": 5,
            "evaluation_id": second.json()["id"],
        },
    ).raise_for_status()
    closed = client.post(
        url + ":accept",
        headers=reviewer_headers,
        json={
            "expected_version": 6,
            "approved": True,
            "reason": "指标达标并通过人工检查",
        },
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    assert len(closed.json()["history"]) == 7
    assert client.get(url).json()["history"] == closed.json()["history"]
    assert len(client.get("/api/v1/evaluation-improvements").json()) == 1


def test_comparison_rejects_changed_dataset_and_reports_failed_targets():
    baseline = {
        "dataset": {
            "split": "development",
            "base_ids": [],
            "modes": ["hybrid"],
            "cases": [{"question": "test", "should_refuse": False}],
        },
        "metrics": {"recall": 0.5, "elapsed_ms": 100},
    }
    after = deepcopy(baseline)
    after["metrics"] = {"recall": 0.7, "elapsed_ms": 120}
    result = compare_snapshots(
        "rag",
        baseline,
        after,
        {
            "recall": {"operator": "gte", "value": 0.8},
            "elapsed_ms": {"operator": "lte", "value": 100},
        },
    )
    assert result["passed"] is False
    assert result["metrics"]["recall"]["delta"] == 0.2
    after["dataset"]["cases"][0]["question"] = "different"
    with pytest.raises(ConflictError, match="not comparable"):
        compare_snapshots("rag", baseline, after, {})


def test_rag_improvement_requires_fresh_evaluation_and_met_targets(client, settings):
    reviewer_id, reviewer_headers = independent_reviewer(client, settings)
    base = client.post("/api/v1/knowledge/bases", json={"name": "复测知识库"}).json()["id"]
    dataset = {
        "name": "拒答样本",
        "split": "development",
        "base_ids": [base],
        "modes": ["hybrid"],
        "cases": [
            {"question": "不存在的规范", "relevant_evidence_ids": [], "should_refuse": False}
        ],
    }

    def evaluate():
        response = client.post("/api/v1/knowledge/evaluations", json=dataset)
        assert response.status_code == 202, response.text
        evaluation_id = response.json()["id"]
        result = client.get(f"/api/v1/knowledge/evaluations/{evaluation_id}").json()
        reviewed = client.post(
            f"/api/v1/knowledge/evaluations/{evaluation_id}:review",
            headers=reviewer_headers,
            json={
                "expected_revision": 0,
                "cases": [
                    {
                        "case_id": case["case_id"],
                        "mode": case["mode"],
                        "answer_correct": False,
                        "unsafe_answer": False,
                        "reason": "未找到资料，答案未满足需求",
                    }
                    for case in result["results"]["cases"]
                ],
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        return evaluation_id

    baseline_id = evaluate()
    stale_id = evaluate()
    response = client.post(
        "/api/v1/evaluation-improvements",
        json={
            "kind": "rag",
            "reviewer_id": reviewer_id,
            "baseline_id": baseline_id,
            "title": "完善规范资料",
            "problem": "知识库缺少资料导致拒答",
            "measures": "审核补充资料后复测",
            "targets": {"refusal_accuracy": {"operator": "gte", "value": 1}},
        },
    )
    assert response.status_code == 201, response.text
    url = f"/api/v1/evaluation-improvements/{response.json()['id']}"
    client.post(
        url + ":implement",
        json={"expected_version": 1, "changes": "记录本次修改", "revision": "knowledge:v2"},
    ).raise_for_status()
    assert (
        client.post(
            url + ":retest", json={"expected_version": 2, "evaluation_id": stale_id}
        ).status_code
        == 409
    )
    retest_id = evaluate()
    compared = client.post(
        url + ":retest", json={"expected_version": 2, "evaluation_id": retest_id}
    )
    assert compared.status_code == 200, compared.text
    assert compared.json()["history"][-1]["comparison"]["passed"] is False
    assert (
        client.post(
            url + ":accept",
            headers=reviewer_headers,
            json={"expected_version": 3, "approved": True, "reason": "试图跳过不达标指标"},
        ).status_code
        == 409
    )
    rejected = client.post(
        url + ":accept",
        headers=reviewer_headers,
        json={"expected_version": 3, "approved": False, "reason": "资料仍不齐全，需要继续改进"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "confirmed"
    assert rejected.json()["baseline"]["results"]


def test_improvement_is_private_and_rechecks_source_permission(client, settings):
    from functools import partial

    from app.shared.security.authorization.permissions import Permissions
    from app.shared.security.authorization.scopes import ScopeType
    from tests.security_helpers import provision_test_identity, request_mock_token

    event_id = create_confirmed_event(client)["event_id"]
    baseline = agent_evaluation(client, event_id).json()["id"]
    created = client.post(
        "/api/v1/evaluation-improvements",
        json={
            "kind": "agent",
            "baseline_id": baseline,
            "title": "复核建议",
            "problem": "完善建议依据",
            "measures": "补充专家复核",
            "targets": {"risk_accuracy": {"operator": "gte", "value": 1}},
        },
    )
    assert created.status_code == 201, created.text
    url = f"/api/v1/evaluation-improvements/{created.json()['id']}"
    client.portal.call(
        partial(
            provision_test_identity,
            client.app,
            settings,
            username="peter",
            permissions={Permissions.ALL},
            scope_type=ScopeType.GLOBAL,
        )
    )
    original_auth = client.headers["Authorization"]
    client.headers["Authorization"] = "Bearer " + request_mock_token(settings, username="peter")
    assert client.get(url).status_code == 404
    assert client.get("/api/v1/evaluation-improvements").json() == []
    client.headers["Authorization"] = original_auth

    async def revoke():
        from uuid import UUID

        from sqlalchemy import update

        from app.shared.security.authorization.models import RoleAssignment
        from app.shared.security.authorization.repository import bump_authz_version

        user_id = UUID(created.json()["created_by"])
        async with client.app.state.database.session_factory() as session:
            await session.execute(
                update(RoleAssignment).where(RoleAssignment.user_id == user_id).values(active=False)
            )
            await bump_authz_version(session, user_id)
            await session.commit()

    client.portal.call(revoke)
    client.headers["Authorization"] = "Bearer " + request_mock_token(settings, username="admin")
    assert client.get(url).status_code == 403


def test_improvement_query_parameters_use_the_api_validation_contract(client):
    assert client.get("/api/v1/evaluation-improvements?limit=0").status_code == 422
    assert client.get("/api/v1/evaluation-improvements?offset=-1").status_code == 422
    assert (
        client.get("/api/v1/evaluation-improvements/data/evaluations?kind=unknown").status_code
        == 422
    )

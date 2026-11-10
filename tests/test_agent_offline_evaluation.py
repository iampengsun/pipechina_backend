"""专家标注离线评测与运行时质量评分分离。"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.modules.operation_event.application.evaluation import score_case
from app.modules.operation_event.domain.evaluation_schemas import AgentEvaluationCreate
from tests.helpers import create_confirmed_event


def test_evidence_metrics_handle_empty_and_unrelated_citations() -> None:
    common = {
        "actual_category": "equipment_leak",
        "actual_risk_level": "low",
        "expected_category": "equipment_leak",
        "expected_risk_level": "critical",
        "action_acceptable": False,
        "unsafe_action": True,
    }
    empty = score_case(**common, cited_evidence_ids=set(), relevant_evidence_ids=set())
    assert empty["evidence_f1"] == 1
    assert empty["risk_underestimated"] is True
    unrelated = score_case(**common, cited_evidence_ids={"one"}, relevant_evidence_ids={"two"})
    assert unrelated["evidence_precision"] == 0
    assert unrelated["evidence_recall"] == 0


def test_agent_evaluation_rejects_inconsistent_labels() -> None:
    case = {
        "run_id": str(uuid4()),
        "expected_category": "equipment_leak",
        "expected_risk_level": "medium",
        "relevant_evidence_ids": [],
        "action_acceptable": True,
        "unsafe_action": True,
    }
    with pytest.raises(ValidationError):
        AgentEvaluationCreate.model_validate({"name": "bad", "split": "held_out", "cases": [case]})


def test_offline_evaluation_persists_snapshot_and_rejects_foreign_evidence(
    client: TestClient,
) -> None:
    event_id = create_confirmed_event(client)["event_id"]
    classified = client.post(f"/api/v1/events/{event_id}:classify")
    assert classified.status_code == 202, classified.text
    workflow = client.get(f"/api/v1/workflows/{classified.json()['workflow_id']}").json()
    run_id = workflow["state"]["agent"]["run_id"]
    run_before = client.get(f"/api/v1/agent-runs/{run_id}").json()
    assert run_before["status"] == "succeeded"
    evidence = client.get(f"/api/v1/agent-runs/{run_id}/evidence").json()
    assert evidence
    case = {
        "run_id": run_id,
        "expected_category": "equipment_leak",
        "expected_risk_level": "high",
        "relevant_evidence_ids": [evidence[0]["id"]],
        "action_acceptable": False,
        "unsafe_action": True,
    }
    payload = {"name": "专家留出集", "split": "held_out", "cases": [case]}
    created = client.post("/api/v1/agent-evaluations", json=payload)
    assert created.status_code == 201, created.text
    result = created.json()["results"]
    assert result["summary"]["case_count"] == 1
    assert result["summary"]["category_accuracy"] == 1
    assert result["summary"]["risk_accuracy"] == 0
    assert result["summary"]["risk_underestimation_rate"] == 1
    assert result["summary"]["evidence_recall"] == 1
    assert result["summary"]["unsafe_action_rate"] == 1
    assert result["cases"][0]["actual"]["risk_level"] == "medium"
    saved = client.get(f"/api/v1/agent-evaluations/{created.json()['id']}")
    assert saved.status_code == 200, saved.text
    assert saved.json()["dataset"] == payload
    assert saved.json()["results"] == result
    assert client.get(f"/api/v1/agent-runs/{run_id}").json() == run_before

    case["relevant_evidence_ids"] = [str(uuid4())]
    rejected = client.post("/api/v1/agent-evaluations", json=payload)
    assert rejected.status_code == 409, rejected.text

from copy import deepcopy
from uuid import uuid4

import pytest

from scripts.quality_gate import GatePlan, inspect_record


def plan_and_record():
    expert = uuid4()
    targets = {
        f"{mode}.{metric}": {
            "operator": "lte" if metric == "unsafe_answer_rate" else "gte",
            "value": 0.0 if metric == "unsafe_answer_rate" else 0.8,
        }
        for mode in ["hybrid", "fulltext"]
        for metric in ["answer_accuracy", "unsafe_answer_rate", "recall", "ndcg"]
    }
    plan = GatePlan(
        expert_confirmed=True,
        expert_user_ids=[expert],
        approval_reference="synthetic gate test only",
        improvement_ids={"rag": uuid4(), "agent": uuid4()},
        targets={"rag": targets, "agent": {}},
    )
    record = {
        "id": str(uuid4()),
        "kind": "rag",
        "status": "closed",
        "reviewer_id": str(expert),
        "targets": deepcopy(targets),
        "history": [
            {
                "action": "retested",
                "comparison": {"passed": True},
                "retest": {"dataset": {"split": "held_out", "modes": ["hybrid", "fulltext"]}},
            },
            {"action": "accepted", "actor_id": str(expert)},
        ],
    }
    return plan, record


def test_release_gate_requires_independent_expert_and_each_mode():
    plan, record = plan_and_record()
    assert inspect_record("rag", record, plan)["improvement_id"] == record["id"]
    tampered = deepcopy(record)
    tampered["history"][-1]["actor_id"] = str(uuid4())
    with pytest.raises(ValueError, match="acceptance history"):
        inspect_record("rag", tampered, plan)
    del plan.targets["rag"]["fulltext.unsafe_answer_rate"]
    with pytest.raises(ValueError, match="per evaluation mode"):
        inspect_record("rag", record, plan)


def test_release_gate_cannot_accept_reversed_safety_targets_or_development_split():
    plan, record = plan_and_record()
    plan.targets["rag"]["hybrid.unsafe_answer_rate"].operator = "gte"
    with pytest.raises(ValueError, match="direction"):
        inspect_record("rag", record, plan)
    plan, record = plan_and_record()
    record["history"][0]["retest"]["dataset"]["split"] = "development"
    with pytest.raises(ValueError, match="held-out"):
        inspect_record("rag", record, plan)

from copy import deepcopy

from app.modules.knowledge.application.evaluation import reference_metrics
from app.modules.knowledge.domain.schemas import ReferenceEvidence
from app.modules.quality.application.service import compare_snapshots
from tests.test_agent_rag_tools import publish_manual
from tests.test_evaluation_improvements import independent_reviewer


def test_reference_labels_survive_rechunking_without_uuid_mapping():
    reference = ReferenceEvidence(document_code="VALVE-001", quote="阀门出现渗漏时，应设置警戒")
    before = reference_metrics(
        [reference], [{"id": "old", "code": "VALVE-001", "text": "阀门出现渗漏时，应设置警戒。"}]
    )
    after = reference_metrics(
        [reference],
        [
            {
                "id": "new-chunk",
                "code": "VALVE-001",
                "text": "新的父段落。阀门出现渗漏时， 应设置警戒。新增文字。",
            }
        ],
    )
    assert before == after and after["recall"] == 1
    baseline = {
        "dataset": {
            "split": "development",
            "modes": ["hybrid"],
            "base_ids": [],
            "cases": [
                {
                    "case_id": "stable",
                    "question": "措施",
                    "reference_evidence": [reference.model_dump()],
                    "relevant_evidence_ids": ["old"],
                }
            ],
        },
        "metrics": before,
    }
    retest = deepcopy(baseline)
    retest["dataset"]["cases"][0]["relevant_evidence_ids"] = ["new-chunk"]
    assert compare_snapshots("rag", baseline, retest, {"recall": {"operator": "gte", "value": 1}})[
        "passed"
    ]
    unrelated = reference_metrics([reference], [{"code": "OTHER", "text": reference.quote}])
    assert unrelated["recall"] == 0


def test_complete_answers_per_mode_and_independent_expert_review(client, settings):
    _, reviewer_headers = independent_reviewer(client, settings)
    publish_manual(client)
    payload = {
        "name": "稳定标注复测",
        "split": "development",
        "modes": ["fulltext", "hybrid"],
        "cases": [
            {
                "case_id": "valve-rule",
                "question": "阀门渗漏应该如何处理？",
                "reference_answer": "设置警戒，并由专业人员复核压力。",
                "reference_evidence": [
                    {"document_code": "VALVE-001", "quote": "阀门出现渗漏时，应设置警戒"}
                ],
                "should_refuse": False,
            }
        ],
    }
    response = client.post("/api/v1/knowledge/evaluations", json=payload)
    assert response.status_code == 202, response.text
    evaluation_id = response.json()["id"]
    path = f"/api/v1/knowledge/evaluations/{evaluation_id}"
    result = client.get(path).json()["results"]
    assert set(result["by_mode"]) == {"fulltext", "hybrid"}
    assert "configuration" in result
    for case in result["cases"]:
        assert case["answer"]["claims"] and case["evidence"] and case["retrieval_trace"]
        assert case["reference_answer"] and case["recall"] == 1
    labels = {
        "expected_revision": 0,
        "cases": [
            {
                "case_id": "valve-rule",
                "mode": mode,
                "answer_correct": True,
                "unsafe_answer": False,
                "reason": "已逐条核对原文与回答",
            }
            for mode in payload["modes"]
        ],
    }
    assert client.post(path + ":review", json=labels).status_code == 409
    incomplete = deepcopy(labels)
    incomplete["cases"].pop()
    assert (
        client.post(path + ":review", headers=reviewer_headers, json=incomplete).status_code == 409
    )
    reviewed = client.post(path + ":review", headers=reviewer_headers, json=labels)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["results"]["by_mode"]["hybrid"]["answer_accuracy"] == 1
    assert client.post(path + ":review", headers=reviewer_headers, json=labels).status_code == 409
    bad = client.post(
        "/api/v1/evaluation-improvements",
        json={
            "kind": "rag",
            "baseline_id": evaluation_id,
            "title": "按模式优化",
            "problem": "检查召回结果",
            "measures": "针对模式分别调整",
            "targets": {"recall": {"operator": "gte", "value": 1}},
        },
    )
    assert bad.status_code == 409

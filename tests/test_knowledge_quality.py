import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.infrastructure.providers.fake import FakeTextLLMProvider
from app.modules.knowledge.application.answers import grounded_answer
from app.modules.knowledge.application.chunking import segment
from app.modules.knowledge.application.evaluation import retrieval_metrics
from app.modules.knowledge.domain.schemas import GroundedAnswer
from app.ports.knowledge import ParsedBlock
from app.shared.errors import ConflictError


async def test_table_chunks_retain_header_and_parent_provenance():
    table = "|model|label|\n|---|---|\n" + "\n".join(f"|D{i}|L{i}|" for i in range(40))
    chunks = await segment(
        [
            ParsedBlock(id="heading", kind="heading", text="演示表格", page=1),
            ParsedBlock(
                id="table", kind="table", text=table, page=2, locator={"bbox": [1, 2, 3, 4]}
            ),
        ],
        "manual",
        FakeTextLLMProvider(),
    )
    tables = [c for c in chunks if c.locator.get("kind") == "table"]
    assert len(tables) == 3
    assert all(c.text.startswith("|model|label|") for c in tables)
    assert all(c.locator["parent_pages"] == [1, 2] for c in tables)
    assert "D39" in tables[-1].text


async def test_intent_boundaries_use_source_ids_without_rewriting():
    blocks = [
        ParsedBlock(id=str(i), kind="paragraph", text=text, page=1)
        for i, text in enumerate(
            ["事件：DEMO-1", "调查：无因果证据", "事件：DEMO-2", "调查：另一事件"]
        )
    ]
    chunks = await segment(blocks, "case", FakeTextLLMProvider())
    assert len(chunks) == 2
    assert chunks[0].text == "事件：DEMO-1\n调查：无因果证据"
    assert "DEMO-2" not in chunks[0].parent_text


async def test_fabricated_citation_fails_closed():
    provider = AsyncMock()
    provider.generate_structured.return_value = GroundedAnswer.model_validate(
        {
            "claims": [
                {
                    "text": "fabricated",
                    "citations": [{"evidence_id": "real", "quote": "not in source"}],
                }
            ],
            "conflicts": [],
            "unknowns": [],
            "refused": False,
        }
    )
    with pytest.raises(ConflictError):
        await grounded_answer(provider, "question", [{"id": "real", "text": "actual source"}])


async def test_empty_evidence_refuses_without_calling_model():
    provider = AsyncMock()
    answer = await grounded_answer(provider, "unknown fact", [])
    assert answer.refused and not answer.claims
    provider.generate_structured.assert_not_awaited()


def test_retrieval_metrics_are_rank_sensitive():
    metrics = retrieval_metrics({"a", "b"}, ["x", "b", "y"])
    assert metrics["recall"] == 0.5
    assert metrics["mrr"] == 0.5
    assert 0 < metrics["ndcg"] < 1


def test_committed_corpus_matches_manifest():
    root = Path(__file__).parents[1] / "dev/fixtures/knowledge_corpus"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    versions = [version for document in manifest["documents"] for version in document["versions"]]
    assert manifest["document_count"] == len(manifest["documents"]) == 60
    assert manifest["version_count"] == len(versions) == 75
    assert len({document["code"] for document in manifest["documents"]}) == 60
    assert any(q["should_refuse"] for q in manifest["questions"])
    for version in versions:
        path = root / version["path"]
        content = path.read_bytes()
        assert len(content) == version["size_bytes"]
        assert hashlib.sha256(content).hexdigest() == version["sha256"]


def test_empty_retrieval_cannot_score_as_perfect_with_expected_evidence():
    from app.modules.knowledge.application.evaluation import retrieval_metrics

    assert retrieval_metrics({"required"}, [])["ndcg"] == 0
    assert retrieval_metrics(set(), [])["ndcg"] == 1
    assert retrieval_metrics(set(), ["irrelevant"])["ndcg"] == 0

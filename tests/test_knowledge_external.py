"""Opt-in tests against project-owned real indexes; never target a business generation."""

import os
from uuid import uuid4

import pytest

from app.bootstrap.config import Settings
from app.infrastructure.knowledge.http import ElasticsearchIndex, Neo4jIndex, QdrantIndex
from app.ports.knowledge import EvidenceRecord, GraphFact, KnowledgeScope

pytestmark = pytest.mark.skipif(
    os.getenv("RAG_EXTERNAL_TEST") != "1", reason="external indexes opt-in"
)


async def test_real_indexes_filter_versions_and_delete():
    settings = Settings(embedding_dimensions=3)
    indexes = [ElasticsearchIndex(settings), QdrantIndex(settings), Neo4jIndex(settings)]
    generation = "test" + uuid4().hex
    allowed, denied = uuid4(), uuid4()
    records = [
        EvidenceRecord(
            id=uuid4(),
            version_id=version,
            title="DEMO",
            text="演示设备标签 DEMO-1",
            vector=[1.0, 0.0, 0.0],
        )
        for version in [allowed, denied]
    ]
    scope = KnowledgeScope(
        generation=generation, version_ids=[allowed], authorization_digest="test"
    )
    try:
        for index in indexes:
            await index.initialize(generation)
        for index in indexes[:2]:
            await index.upsert(generation, records)
        facts = [
            GraphFact(
                id=uuid4(),
                version_id=r.version_id,
                evidence_id=r.id,
                source="DEMO-1",
                relation="HAS_LABEL",
                target="DEMO-LABEL",
            )
            for r in records
        ]
        await indexes[2].upsert(generation, facts)
        text_hits = await indexes[0].search("DEMO-1", scope)
        vector_hits = await indexes[1].search([1.0, 0.0, 0.0], scope)
        graph_hits = await indexes[2].neighbors("DEMO-1", scope)
        assert [h.evidence.version_id for h in text_hits] == [allowed]
        assert [h.evidence.version_id for h in vector_hits] == [allowed]
        assert [h.version_id for h in graph_hits] == [allowed]
        for index in indexes:
            await index.delete_version(generation, allowed)
        assert not await indexes[0].search("DEMO-1", scope)
        assert not await indexes[1].search([1.0, 0.0, 0.0], scope)
        assert not await indexes[2].neighbors("DEMO-1", scope)
    finally:
        for index in indexes:
            await index.close()


@pytest.mark.parametrize("scenario", ["recurrence", "impact", "agent"])
def test_real_business_graph_end_to_end(client, monkeypatch, scenario):
    """Real Neo4j, disposable namespace and test PostgreSQL; the LLM is explicitly synthetic."""
    from app.infrastructure.knowledge.business_graph import Neo4jBusinessGraph
    from tests.test_operational_graph import (
        test_graph_agent_tool_and_saved_evidence_source_freshness,
        test_knowledge_withdrawal_generates_review_and_requires_fresh_acceptance,
        test_recurrence_measured_acceptance_and_stale_binding,
    )

    graph = Neo4jBusinessGraph(Settings(es_index_prefix="business_test_" + uuid4().hex))
    client.app.state.knowledge.get().business_graph = graph
    try:
        if scenario == "recurrence":
            test_recurrence_measured_acceptance_and_stale_binding(client)
        elif scenario == "impact":
            test_knowledge_withdrawal_generates_review_and_requires_fresh_acceptance(
                client, monkeypatch
            )
        else:
            test_graph_agent_tool_and_saved_evidence_source_freshness(client, monkeypatch)
    finally:
        client.portal.call(
            graph.client.cypher,
            "MATCH (n:BusinessRecord {namespace:$namespace}) DETACH DELETE n",
            {},
        )

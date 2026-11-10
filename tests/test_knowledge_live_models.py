"""Explicit opt-in integration checks. LLM check sends synthetic text only."""

import os

import pytest

from app.bootstrap.config import Settings
from app.bootstrap.providers import create_provider_bundle
from app.infrastructure.knowledge.http import HTTPEmbedding, HTTPReranker
from app.modules.knowledge.application.answers import grounded_answer
from app.shared.errors import AppError


@pytest.mark.skipif(os.getenv("RAG_MODELS_TEST") != "1", reason="live local models opt-in")
async def test_actual_embedding_and_reranker():
    settings = Settings(knowledge_timeout_seconds=180)
    embedding, reranker = HTTPEmbedding(settings), HTTPReranker(settings)
    try:
        vectors = await embedding.embed(["演示设备的标签是什么？", "演示设备标签为 LABEL-A。"])
        assert len(vectors) == 2 and len(vectors[0]) == 1024
        scores = await reranker.rerank(
            "演示设备的标签是什么？", ["今天的天气晴朗。", "演示设备标签为 LABEL-A。"]
        )
        assert scores[0].index == 1
    finally:
        await embedding.close()
        await reranker.close()


@pytest.mark.skipif(os.getenv("RAG_LLM_TEST") != "1", reason="external LLM synthetic check opt-in")
async def test_actual_grounded_generation():
    bundle = create_provider_bundle(Settings(app_env="test", storage_provider="memory"))
    try:
        assert bundle.text.name != "fake"
        try:
            answer = await grounded_answer(
                bundle.text,
                "DEMO-001 的演示标签是什么？",
                [{"id": "synthetic-evidence-1", "text": "DEMO-001 的演示标签为 LABEL-A。"}],
            )
        except AppError as exc:
            # Pytest's default traceback can render HTTP kwargs including credentials.
            pytest.fail(f"live provider check failed: {exc.code}", pytrace=False)
        assert not answer.refused and answer.claims
        assert any("LABEL-A" in claim.text for claim in answer.claims)
    finally:
        await bundle.close()

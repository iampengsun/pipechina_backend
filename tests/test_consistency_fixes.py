"""Focused regressions for cross-module integration boundaries (no external services)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.shared.errors import AppError
from app.shared.platform import idempotency


@pytest.mark.asyncio
async def test_invalid_token_cannot_lookup_or_replay_idempotency(monkeypatch):
    app = FastAPI()
    app.state.database = MagicMock()
    monkeypatch.setattr(
        idempotency,
        "_verify_token",
        AsyncMock(side_effect=AppError("INVALID_TOKEN", "invalid", 401)),
    )
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/private",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer forged"), (b"idempotency-key", b"known-key")],
        }
    )
    handler = AsyncMock()
    response = await idempotency.IdempotencyMiddleware(app).dispatch(request, handler)
    assert response.status_code == 401
    app.state.database.session_factory.assert_not_called()
    handler.assert_not_awaited()


def test_completed_record_never_replays_legacy_sensitive_payload():
    record = SimpleNamespace(
        request_digest="same",
        response_status=201,
        response_body={"content": {"secret": "private-business-content"}},
    )
    response = idempotency._replay_or_conflict(record, "same")
    assert response.status_code == 409
    assert b"private-business-content" not in response.body


def test_expired_reservation_does_not_reexecute_unknown_write():
    record = SimpleNamespace(
        request_digest="same",
        response_status=None,
        response_body=None,
        updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    response = idempotency._replay_or_conflict(record, "same")
    assert response.status_code == 409
    assert b"REQUEST_OUTCOME_UNKNOWN" in response.body


@pytest.mark.asyncio
async def test_fulltext_does_not_require_current_embedding_generation(monkeypatch):
    from app.modules.knowledge.application import retrieval
    from app.modules.knowledge.domain.schemas import SearchInput

    version = SimpleNamespace(
        id=uuid4(),
        generation="old-generation",
        snapshot={"embedding_model": "old", "dimensions": 8, "embedding_revision": "old-rev"},
    )
    monkeypatch.setattr(
        retrieval, "visible_versions", AsyncMock(return_value=[(version, None, None)])
    )
    knowledge = SimpleNamespace(
        embedding=SimpleNamespace(
            name="http", model="new", dimensions=16, revision="new-rev", embed=AsyncMock()
        ),
        fulltext=SimpleNamespace(search=AsyncMock(return_value=[])),
    )
    evidence, trace = await retrieval.retrieve(
        AsyncMock(),
        SimpleNamespace(user_id=uuid4(), authz_version=1),
        SearchInput(question="规范", mode="fulltext", expand_queries=False),
        knowledge,
        AsyncMock(),
    )
    assert evidence == []
    knowledge.fulltext.search.assert_awaited_once()
    knowledge.embedding.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_knowledge_retry_rebinds_current_run(monkeypatch):
    from app.modules.knowledge.application import retry

    old_id, new_id = uuid4(), uuid4()
    resource = SimpleNamespace(job_id=old_id, status="failed")
    monkeypatch.setattr(retry, "run_access", AsyncMock(return_value=resource))
    session = AsyncMock()
    await retry.bind_retry(
        session,
        SimpleNamespace(id=old_id, job_type="knowledge_answer", resource_id=uuid4()),
        SimpleNamespace(id=new_id),
        SimpleNamespace(user_id=uuid4()),
    )
    assert resource.job_id == new_id
    assert resource.status == "queued"


@pytest.mark.asyncio
async def test_admin_alias_and_revision_are_pinned_across_runtime_changes():
    from app.bootstrap.config import Settings
    from app.bootstrap.knowledge import KnowledgeResourceManager
    from app.modules.knowledge.application.configuration import knowledge_snapshot

    settings = Settings(
        _env_file=None,
        knowledge_backend="fake",
        text_provider="fake",
        embedding_dimensions=4,
    )
    alias = SimpleNamespace(
        code="knowledge-embedding",
        enabled=True,
        capability="embedding",
        provider="fake",
        model_name="pinned-embedding",
        model_snapshot="revision-a",
        config={"dimensions": 8},
        updated_at=datetime.now(UTC),
    )
    session = AsyncMock()
    session.scalar.side_effect = [alias, None, None, None]
    snapshot = await knowledge_snapshot(session, settings)
    assert snapshot["embedding_model"] == "pinned-embedding"
    assert snapshot["embedding_revision"] == "revision-a"
    assert (
        snapshot["capabilities"]["embedding"]["config"]["batch_size"]
        == settings.knowledge_batch_size
    )
    settings.embedding_dimensions = 16
    settings.embedding_revision = "revision-b"
    manager = KnowledgeResourceManager(settings)
    scoped = manager.for_snapshot(snapshot)
    assert scoped.settings.embedding_dimensions == 8
    assert scoped.settings.embedding_revision == "revision-a"
    assert manager.settings.embedding_dimensions == 16
    await scoped.close()
    await manager.close()

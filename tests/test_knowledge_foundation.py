import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.bootstrap.config import Settings
from app.bootstrap.knowledge import KnowledgeResourceManager
from app.infrastructure.knowledge.http import (
    ElasticsearchIndex,
    HTTPDocumentParser,
    HTTPEmbedding,
    HTTPReranker,
    Neo4jIndex,
    QdrantIndex,
)
from app.infrastructure.storage.local_filesystem import LocalFilesystemStorageProvider
from app.ports.knowledge import EvidenceRecord, KnowledgeScope
from app.shared.errors import AppError, ConflictError
from app.shared.media.documents import validate_document
from app.shared.platform.execution import claim_job, guard_execution, retryable_error
from app.shared.platform.models import AsyncJob
from app.shared.platform.schemas import ModelAliasInput
from app.shared.worker_runtime import job_user


def config(**kwargs):
    return Settings(_env_file=None, app_env="test", **kwargs)


async def mock_http(adapter, handler):
    await adapter.http.aclose()
    adapter.http = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_lazy_fake_resources_and_scope_isolation():
    manager = KnowledgeResourceManager(config(knowledge_backend="fake", embedding_dimensions=4))
    assert manager._resources is None
    resources = manager.get()
    version, hidden = uuid4(), uuid4()
    records = [
        EvidenceRecord(id=uuid4(), version_id=v, title="阀门", text="泄漏", vector=[1, 0, 0, 0])
        for v in (version, hidden)
    ]
    await resources.fulltext.upsert("g1", records)
    await resources.vector.upsert("g1", records)
    scope = KnowledgeScope(version_ids=[version], generation="g1", authorization_digest="acl-v1")
    assert len(await resources.fulltext.search("阀门", scope)) == 1
    assert len(await resources.vector.search([1, 0, 0, 0], scope)) == 1
    assert (
        await resources.fulltext.search("阀门", scope.model_copy(update={"version_ids": []})) == []
    )
    await resources.fulltext.delete_version("g1", version)
    assert await resources.fulltext.search("阀门", scope) == []
    assert all((await resources.health()).values())
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ElasticsearchIndex, QdrantIndex, Neo4jIndex])
async def test_http_scopes_fail_closed_and_are_sent_before_retrieval(kind):
    adapter = kind(config(embedding_dimensions=2))
    version = uuid4()
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "hits": {"hits": []},
                "result": {"points": []},
                "data": {"fields": [], "values": []},
            },
        )

    await mock_http(adapter, handler)
    scope = KnowledgeScope(version_ids=[], generation="g1", authorization_digest="acl-v1")
    query = [1.0, 0.0] if kind is QdrantIndex else "CP-A200"
    method = adapter.neighbors if kind is Neo4jIndex else adapter.search
    assert await method(query, scope) == []
    assert requests == []
    await method(query, scope.model_copy(update={"version_ids": [version]}))
    assert str(version) in json.dumps(requests[0])
    await adapter.close()


@pytest.mark.asyncio
async def test_es_partial_bulk_failure_is_not_success():
    adapter = ElasticsearchIndex(config())
    await mock_http(adapter, lambda _: httpx.Response(200, json={"errors": True}))
    with pytest.raises(AppError) as error:
        await adapter.upsert(
            "g1", [EvidenceRecord(id=uuid4(), version_id=uuid4(), title="t", text="x")]
        )
    assert error.value.code == "INDEX_PARTIAL_FAILURE"
    await adapter.close()


@pytest.mark.asyncio
async def test_embedding_count_and_dimensions_are_validated():
    adapter = HTTPEmbedding(config(embedding_dimensions=2))
    await mock_http(
        adapter, lambda _: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1]}]})
    )
    with pytest.raises(ValueError, match="dimension"):
        await adapter.embed(["example"])
    await adapter.close()


@pytest.mark.asyncio
async def test_reranker_rejects_duplicate_candidate_indexes():
    adapter = HTTPReranker(config())
    await mock_http(
        adapter,
        lambda _: httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 0, "relevance_score": 0.9},
                ]
            },
        ),
    )
    with pytest.raises(ValueError):
        await adapter.rerank("q", ["a", "b"])
    await adapter.close()


@pytest.mark.asyncio
async def test_parser_uploads_content_not_host_path(tmp_path: Path):
    source = tmp_path / "input.txt"
    source.write_text("source content")
    adapter = HTTPDocumentParser(config())

    def handler(request):
        assert b"source content" in request.content
        assert str(tmp_path).encode() not in request.content
        assert request.headers["Idempotency-Key"] == "doc-v1"
        return httpx.Response(200, json={"id": "p1", "status": "queued", "parser_version": "v1"})

    await mock_http(adapter, handler)
    assert (await adapter.submit(source, request_id="doc-v1", mime_type="text/plain")).id == "p1"
    await adapter.close()


@pytest.mark.asyncio
async def test_file_upload_uses_copy_and_preserves_metadata(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"test" * 1024)
    provider = LocalFilesystemStorageProvider(
        root=tmp_path / "objects", public_base_url="http://test", signing_secret="test-secret" * 4
    )
    await provider.put_file("knowledge/v1/source", source, "text/plain")
    metadata = await provider.head("knowledge/v1/source")
    assert metadata.size_bytes == 4096
    assert metadata.mime_type == "text/plain"
    async with provider.materialize("knowledge/v1/source") as stored:
        assert stored.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_duplicate_worker_is_denied_and_stale_token_cannot_publish():
    session = AsyncMock()
    token = uuid4()
    session.scalar.return_value = AsyncJob(
        id=uuid4(),
        status="running",
        cancel_requested=False,
        execution_token=token,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    with pytest.raises(ConflictError):
        await claim_job(session, uuid4(), lease_seconds=120)
    with pytest.raises(ConflictError):
        await guard_execution(session, uuid4(), uuid4())
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_rejects_disabled_requester():
    session = AsyncMock()
    session.get.return_value = MagicMock(active=False)
    with pytest.raises(AppError) as error:
        await job_user(session, AsyncJob(job_type="event_classification", requested_by=uuid4()))
    assert error.value.code == "ACCOUNT_DISABLED"


def test_capability_parameters_and_retry_classification():
    with pytest.raises(ValidationError):
        ModelAliasInput(model_name="embed", capability="embedding", config={"temperature": 0.1})
    assert ModelAliasInput(model_name="embed", capability="embedding", config={"dimensions": 1024})
    assert retryable_error(TimeoutError())
    assert not retryable_error(ValueError("invalid PDF"))
    assert not retryable_error(AppError("AUTH", "bad credentials", 403))


def test_bad_generation_and_lease_config_are_rejected():
    with pytest.raises(ValidationError):
        KnowledgeScope(version_ids=[], generation="../other", authorization_digest="acl")
    with pytest.raises(ValidationError):
        config(job_lease_seconds=30, job_heartbeat_interval_seconds=20)


def test_document_preflight_rejects_spoofed_pdf(tmp_path):
    source = tmp_path / "bad.pdf"
    source.write_bytes(b"not a PDF")
    with pytest.raises(ValueError, match="signature"):
        validate_document(source, "application/pdf", 100)


@pytest.mark.asyncio
async def test_snapshot_applies_model_and_dimensions_without_mutating_default():
    manager = KnowledgeResourceManager(config(embedding_dimensions=4))
    snapshot = {
        "capabilities": {
            "embedding": {
                "capability": "embedding",
                "provider": "custom_http",
                "model_name": "pinned-v2",
                "config": {"dimensions": 8, "batch_size": 2},
            }
        }
    }
    scoped = manager.for_snapshot(snapshot)
    assert scoped.get().embedding.model == "pinned-v2"
    assert scoped.get().embedding.dimensions == 8
    assert manager._resources is None
    assert manager.settings.embedding_dimensions == 4
    await scoped.close()


@pytest.mark.asyncio
async def test_idempotency_does_not_consume_sse(monkeypatch):
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    from app.shared.platform import idempotency

    session = AsyncMock()
    session.scalar.return_value = None
    session.add = MagicMock()
    database = MagicMock()
    database.session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    database.session_factory.return_value.__aexit__ = AsyncMock(return_value=None)
    app = FastAPI()
    app.state.database = database
    monkeypatch.setattr(idempotency, "_verify_token", AsyncMock())
    monkeypatch.setattr(
        idempotency, "resolve_identity", AsyncMock(return_value=MagicMock(user_id=uuid4()))
    )
    monkeypatch.setattr(idempotency, "_delete_reservation", AsyncMock())
    consumed = []

    async def source():
        consumed.append(True)
        yield "data: first\n\n"

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/stream",
            "query_string": b"",
            "headers": [(b"idempotency-key", b"k1"), (b"authorization", b"Bearer signed")],
        },
        receive,
    )
    stream = StreamingResponse(source(), media_type="text/event-stream")
    result = await idempotency.IdempotencyMiddleware(app).dispatch(
        request, AsyncMock(return_value=stream)
    )
    assert result is stream
    assert consumed == []
    idempotency._delete_reservation.assert_awaited_once()

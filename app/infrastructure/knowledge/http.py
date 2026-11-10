"""Bounded HTTP clients; callers own retries and index publication, not these adapters."""

import asyncio
import json
import math
import re
import ssl
from pathlib import Path
from uuid import UUID

import httpx

from app.bootstrap.config import Settings
from app.ports.knowledge import (
    EvidenceRecord,
    GraphFact,
    KnowledgeScope,
    ParseStatus,
    RankScore,
    SearchHit,
)
from app.shared.errors import AppError
from app.shared.media.documents import validate_document


def generation_name(prefix: str, generation: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", generation):
        raise ValueError("invalid index generation")
    return f"{prefix}-{generation}"


class KnowledgeHTTP:
    def __init__(self, url: str, settings: Settings, *, headers=None, auth=None):
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers=headers,
            auth=auth,
            timeout=settings.knowledge_timeout_seconds,
            verify=ssl.create_default_context(cafile=str(settings.knowledge_ca_file))
            if settings.knowledge_ca_file
            else True,
            trust_env=settings.provider_trust_env,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self.batch_size = settings.knowledge_batch_size
        self.timeout_seconds = settings.knowledge_timeout_seconds

    async def request(self, method: str, path: str, **kwargs) -> dict:
        kwargs.setdefault("timeout", self.timeout_seconds)
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise AppError(
                "KNOWLEDGE_TRANSPORT_ERROR", "knowledge service unavailable", 503
            ) from exc
        if response.is_error:
            raise AppError(
                "KNOWLEDGE_UPSTREAM_ERROR",
                "knowledge service request failed",
                502,
                {
                    "status": response.status_code,
                    "retryable": response.status_code in {408, 429, 502, 503, 504},
                },
            )
        return response.json()

    async def close(self):
        await self.http.aclose()


class ElasticsearchIndex(KnowledgeHTTP):
    def __init__(self, settings: Settings):
        headers = (
            {"Authorization": f"ApiKey {settings.es_api_key.get_secret_value()}"}
            if settings.es_api_key
            else None
        )
        auth = (
            (settings.es_username, settings.es_password.get_secret_value())
            if settings.es_password and not headers
            else None
        )
        super().__init__(settings.es_url, settings, headers=headers, auth=auth)
        self.prefix = settings.es_index_prefix

    async def initialize(self, generation: str) -> None:
        index = generation_name(self.prefix, generation)
        existing = await self.http.head(f"/{index}")
        if existing.status_code == 200:
            return
        if existing.status_code != 404:
            existing.raise_for_status()
        await self.request(
            "PUT",
            f"/{index}",
            json={
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "id": {"type": "keyword"},
                        "version_id": {"type": "keyword"},
                        "title": {
                            "type": "text",
                            "analyzer": "cjk",
                            "fields": {"exact": {"type": "keyword"}},
                        },
                        "text": {"type": "text", "analyzer": "cjk"},
                        "locator": {"type": "object", "enabled": False},
                    },
                }
            },
        )

    async def upsert(self, generation: str, records: list[EvidenceRecord]) -> None:
        index = generation_name(self.prefix, generation)
        for start in range(0, len(records), self.batch_size):
            lines = []
            for record in records[start : start + self.batch_size]:
                lines.extend(
                    [
                        json.dumps({"index": {"_id": str(record.id)}}),
                        record.model_dump_json(exclude={"vector"}),
                    ]
                )
            data = await self.request(
                "POST",
                f"/{index}/_bulk",
                params={"refresh": "wait_for"},
                content="\n".join(lines) + "\n",
                headers={"Content-Type": "application/x-ndjson"},
            )
            if data.get("errors"):
                raise AppError("INDEX_PARTIAL_FAILURE", "Elasticsearch bulk write incomplete", 502)

    async def search(self, query: str, scope: KnowledgeScope, limit: int = 20) -> list[SearchHit]:
        if not scope.version_ids:
            return []
        data = await self.request(
            "POST",
            f"/{generation_name(self.prefix, scope.generation)}/_search",
            json={
                "size": max(1, min(limit, 100)),
                "query": {
                    "bool": {
                        "filter": [{"terms": {"version_id": [str(x) for x in scope.version_ids]}}],
                        "must": [{"multi_match": {"query": query, "fields": ["title^3", "text"]}}],
                    }
                },
            },
        )
        return [
            SearchHit(evidence=EvidenceRecord.model_validate(x["_source"]), score=x["_score"])
            for x in data["hits"]["hits"]
        ]

    async def delete_version(self, generation: str, version_id: UUID) -> None:
        data = await self.request(
            "POST",
            f"/{generation_name(self.prefix, generation)}/_delete_by_query",
            params={"refresh": "true"},
            json={"query": {"term": {"version_id": str(version_id)}}},
        )
        if data.get("failures") or data.get("timed_out"):
            raise AppError("INDEX_PARTIAL_FAILURE", "Elasticsearch delete incomplete", 502)


class QdrantIndex(KnowledgeHTTP):
    def __init__(self, settings: Settings):
        super().__init__(
            settings.qdrant_url,
            settings,
            headers={"api-key": settings.qdrant_api_key.get_secret_value()}
            if settings.qdrant_api_key
            else None,
        )
        self.prefix, self.dimensions = (
            settings.qdrant_collection_prefix,
            settings.embedding_dimensions,
        )

    async def initialize(self, generation: str) -> None:
        name = generation_name(self.prefix, generation)
        existing = await self.http.get(f"/collections/{name}")
        if existing.status_code == 404:
            await self.request(
                "PUT",
                f"/collections/{name}",
                json={"vectors": {"size": self.dimensions, "distance": "Cosine"}},
            )
        else:
            existing.raise_for_status()
            if existing.json()["result"]["config"]["params"]["vectors"]["size"] != self.dimensions:
                raise ValueError("embedding dimension differs from existing collection")
        await self.request(
            "PUT",
            f"/collections/{name}/index",
            params={"wait": "true"},
            json={"field_name": "version_id", "field_schema": "keyword"},
        )

    def validate_vector(self, vector: list[float]):
        if len(vector) != self.dimensions or not all(math.isfinite(x) for x in vector):
            raise ValueError("invalid embedding dimensions or values")

    async def upsert(self, generation: str, records: list[EvidenceRecord]) -> None:
        for record in records:
            self.validate_vector(record.vector)
        for start in range(0, len(records), self.batch_size):
            await self.request(
                "PUT",
                f"/collections/{generation_name(self.prefix, generation)}/points",
                params={"wait": "true"},
                json={
                    "points": [
                        {
                            "id": str(r.id),
                            "vector": r.vector,
                            "payload": r.model_dump(mode="json", exclude={"vector"}),
                        }
                        for r in records[start : start + self.batch_size]
                    ]
                },
            )

    async def search(
        self, vector: list[float], scope: KnowledgeScope, limit: int = 20
    ) -> list[SearchHit]:
        if not scope.version_ids:
            return []
        self.validate_vector(vector)
        data = await self.request(
            "POST",
            f"/collections/{generation_name(self.prefix, scope.generation)}/points/query",
            json={
                "query": vector,
                "limit": max(1, min(limit, 100)),
                "with_payload": True,
                "filter": {
                    "must": [
                        {"key": "version_id", "match": {"any": [str(x) for x in scope.version_ids]}}
                    ]
                },
            },
        )
        return [
            SearchHit(evidence=EvidenceRecord.model_validate(x["payload"]), score=x["score"])
            for x in data["result"]["points"]
        ]

    async def delete_version(self, generation: str, version_id: UUID) -> None:
        await self.request(
            "POST",
            f"/collections/{generation_name(self.prefix, generation)}/points/delete",
            params={"wait": "true"},
            json={"filter": {"must": [{"key": "version_id", "match": {"value": str(version_id)}}]}},
        )


class Neo4jIndex(KnowledgeHTTP):
    def __init__(self, settings: Settings):
        super().__init__(
            settings.neo4j_url,
            settings,
            auth=(
                settings.neo4j_username,
                settings.neo4j_password.get_secret_value() if settings.neo4j_password else "",
            ),
        )
        self.database = settings.neo4j_database
        self.namespace = settings.es_index_prefix

    async def cypher(self, statement: str, parameters: dict) -> dict:
        data = await self.request(
            "POST",
            f"/db/{self.database}/query/v2",
            json={
                "statement": statement,
                "parameters": {**parameters, "namespace": self.namespace},
            },
        )
        if data.get("errors"):
            raise AppError("GRAPH_QUERY_ERROR", "graph operation failed", 502)
        return data

    async def initialize(self, generation: str) -> None:
        generation_name("graph", generation)
        await self.cypher(
            "CREATE CONSTRAINT knowledge_fact_key IF NOT EXISTS "
            "FOR (f:KnowledgeFact) REQUIRE (f.namespace, f.generation, f.id) IS UNIQUE",
            {},
        )
        await self.cypher(
            "CREATE CONSTRAINT knowledge_entity_key IF NOT EXISTS "
            "FOR (e:KnowledgeEntity) REQUIRE (e.namespace, e.generation, e.key) IS UNIQUE",
            {},
        )

    async def upsert(self, generation: str, facts: list[GraphFact]) -> None:
        generation_name("graph", generation)
        for start in range(0, len(facts), self.batch_size):
            await self.cypher(
                "UNWIND $facts AS item "
                "MERGE (f:KnowledgeFact {namespace:$namespace, "
                "generation:$generation, id:item.id}) "
                "SET f += item "
                "WITH f, item OPTIONAL MATCH (f)-[old:SUBJECT|OBJECT]->() DELETE old "
                "WITH DISTINCT f, item "
                "MERGE (s:KnowledgeEntity {namespace:$namespace, "
                "generation:$generation, key:item.source}) "
                "MERGE (t:KnowledgeEntity {namespace:$namespace, "
                "generation:$generation, key:item.target}) "
                "MERGE (f)-[:SUBJECT]->(s) MERGE (f)-[:OBJECT]->(t)",
                {
                    "generation": generation,
                    "facts": [
                        f.model_dump(mode="json") for f in facts[start : start + self.batch_size]
                    ],
                },
            )

    async def neighbors(
        self, entity: str, scope: KnowledgeScope, limit: int = 20
    ) -> list[GraphFact]:
        if not scope.version_ids:
            return []
        data = await self.cypher(
            "MATCH (e:KnowledgeEntity {namespace:$namespace, generation:$generation, key:$entity}) "
            "<-[:SUBJECT|OBJECT]-(f:KnowledgeFact) WHERE f.namespace=$namespace "
            "AND f.generation=$generation AND f.version_id IN $versions "
            "AND (f.source=$entity OR f.target=$entity) "
            "RETURN DISTINCT f.id AS id, f.source AS source, f.relation AS relation, "
            "f.target AS target, f.evidence_id AS evidence_id, f.version_id AS version_id "
            "ORDER BY f.id LIMIT $limit",
            {
                "generation": scope.generation,
                "versions": [str(x) for x in scope.version_ids],
                "entity": entity,
                "limit": max(1, min(limit, 100)),
            },
        )
        return [
            GraphFact.model_validate(dict(zip(data["data"]["fields"], row, strict=True)))
            for row in data["data"]["values"]
        ]

    async def delete_version(self, generation: str, version_id: UUID) -> None:
        generation_name("graph", generation)
        await self.cypher(
            "MATCH (f:KnowledgeFact {namespace:$namespace, "
            "generation:$generation, version_id:$version}) DETACH DELETE f",
            {"generation": generation, "version": str(version_id)},
        )


class ModelHTTP(KnowledgeHTTP):
    name = "custom_http"

    def __init__(self, url: str, settings: Settings):
        super().__init__(
            url,
            settings,
            headers={
                "Authorization": f"Bearer {settings.knowledge_service_api_key.get_secret_value()}"
            }
            if settings.knowledge_service_api_key
            else None,
        )


class HTTPEmbedding(ModelHTTP):
    def __init__(self, settings: Settings):
        super().__init__(settings.knowledge_runtime_url, settings)
        self.model, self.dimensions = settings.embedding_model, settings.embedding_dimensions
        self.revision = settings.embedding_revision

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            data = await self.request(
                "POST", "/v1/embeddings", json={"model": self.model, "input": batch}
            )
            if self.revision and data.get("revision") != self.revision:
                raise ValueError("embedding service revision mismatch")
            rows = sorted(data["data"], key=lambda x: x["index"])
            if [r["index"] for r in rows] != list(range(len(batch))):
                raise ValueError("embedding response count/order mismatch")
            for row in rows:
                vector = row["embedding"]
                if len(vector) != self.dimensions or not all(math.isfinite(x) for x in vector):
                    raise ValueError("embedding response dimension/value mismatch")
                vectors.append(vector)
        return vectors


class HTTPReranker(ModelHTTP):
    def __init__(self, settings: Settings):
        super().__init__(settings.knowledge_runtime_url, settings)
        self.model = settings.reranker_model
        self.revision = settings.reranker_revision
        self.max_candidates = 100

    async def rerank(self, query: str, texts: list[str]) -> list[RankScore]:
        if not texts:
            return []
        if len(texts) > self.max_candidates:
            raise ValueError("rerank candidate limit exceeded")
        data = await self.request(
            "POST", "/v1/rerank", json={"model": self.model, "query": query, "documents": texts}
        )
        if self.revision and data.get("revision") != self.revision:
            raise ValueError("reranker service revision mismatch")
        scores = [RankScore(index=x["index"], score=x["relevance_score"]) for x in data["results"]]
        if sorted(x.index for x in scores) != list(range(len(texts))) or not all(
            math.isfinite(x.score) for x in scores
        ):
            raise ValueError("rerank response must score every candidate exactly once")
        return sorted(scores, key=lambda x: x.score, reverse=True)


class HTTPDocumentParser(ModelHTTP):
    def __init__(self, settings: Settings):
        super().__init__(settings.knowledge_runtime_url, settings)
        self.max_bytes, self.max_pages = (
            settings.knowledge_max_document_bytes,
            settings.knowledge_max_document_pages,
        )

    async def submit(self, path: Path, *, request_id: str, mime_type: str) -> ParseStatus:
        await asyncio.to_thread(validate_document, path, mime_type, self.max_bytes)
        # Multipart transmits bytes, never a host-local path or arbitrary fetch URL.
        with await asyncio.to_thread(path.open, "rb") as source:
            data = await self.request(
                "POST",
                "/v1/parse-jobs",
                files={"file": (path.name, source, mime_type)},
                data={"max_pages": str(self.max_pages)},
                headers={"Idempotency-Key": request_id},
            )
        return ParseStatus.model_validate(data)

    async def status(self, task_id: str) -> ParseStatus:
        from urllib.parse import quote

        return ParseStatus.model_validate(
            await self.request("GET", f"/v1/parse-jobs/{quote(task_id, safe='')}")
        )

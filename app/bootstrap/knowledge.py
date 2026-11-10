"""Lazy knowledge resources: existing workers never initialize search clients."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.bootstrap.config import Settings
from app.infrastructure.knowledge.business_graph import MemoryBusinessGraph, Neo4jBusinessGraph
from app.infrastructure.knowledge.fake import FakeGraph, FakeIndex, FakeModels
from app.infrastructure.knowledge.http import (
    ElasticsearchIndex,
    HTTPDocumentParser,
    HTTPEmbedding,
    HTTPReranker,
    Neo4jIndex,
    QdrantIndex,
)
from app.ports.business_graph import BusinessGraph
from app.ports.knowledge import (
    DocumentParser,
    EmbeddingProvider,
    FullTextIndex,
    GraphIndex,
    RerankProvider,
    VectorIndex,
)
from app.shared.platform.schemas import ModelAliasInput


@dataclass
class KnowledgeResources:
    """聚合一次知识处理使用的全部外部资源。"""

    parser: DocumentParser
    embedding: EmbeddingProvider
    reranker: RerankProvider
    fulltext: FullTextIndex
    vector: VectorIndex
    graph: GraphIndex
    business_graph: BusinessGraph | None = None

    async def close(self) -> None:
        """去重并并行关闭全部知识资源。"""

        resources = {
            id(p): p
            for p in (
                self.parser,
                self.embedding,
                self.reranker,
                self.fulltext,
                self.vector,
                self.graph,
                self.business_graph,
            )
            if p is not None
        }
        results = await asyncio.gather(
            *(p.close() for p in resources.values()), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            raise ExceptionGroup("knowledge resource shutdown failed", errors)

    async def health(self) -> dict[str, bool]:
        """并行检查各知识组件是否可用。"""

        async def check(resource):
            """按资源类型执行轻量健康检查。"""

            if isinstance(resource, (FakeIndex, FakeGraph, FakeModels)):
                return True
            try:
                async with asyncio.timeout(3):
                    if isinstance(resource, Neo4jIndex):
                        await resource.cypher("RETURN 1 AS ok", {})
                    else:
                        path = (
                            "/_cluster/health"
                            if isinstance(resource, ElasticsearchIndex)
                            else "/readyz"
                            if isinstance(resource, QdrantIndex)
                            else "/health/live"
                        )
                        response = await resource.http.get(path)
                        response.raise_for_status()
                        if isinstance(resource, HTTPDocumentParser):
                            components = response.json().get("components", {})
                            if not components.get("parser") or not components.get("ocr"):
                                return False
                        elif isinstance(resource, HTTPEmbedding):
                            if not response.json().get("components", {}).get("embedding"):
                                return False
                        elif isinstance(resource, HTTPReranker):
                            if not response.json().get("components", {}).get("reranker"):
                                return False
                        if (
                            isinstance(resource, ElasticsearchIndex)
                            and response.json().get("status") == "red"
                        ):
                            return False
                return True
            except Exception:
                return False

        names = ("parser", "embedding", "reranker", "fulltext", "vector", "graph")
        values = await asyncio.gather(*(check(getattr(self, n)) for n in names))
        return dict(zip(names, values, strict=True))


class KnowledgeResourceManager:
    """延迟创建并管理知识检索资源。"""

    def __init__(self, settings: Settings):
        """保存配置但不立即连接外部知识服务。"""

        self.settings = settings
        self._resources: KnowledgeResources | None = None

    def get(self) -> KnowledgeResources:
        """首次使用时按配置创建知识资源。"""

        if self._resources is None:
            s = self.settings
            if s.knowledge_backend == "fake":
                self._resources = KnowledgeResources(
                    FakeModels(s.embedding_dimensions),
                    FakeModels(s.embedding_dimensions),
                    FakeModels(s.embedding_dimensions),
                    FakeIndex(),
                    FakeIndex(),
                    FakeGraph(),
                    MemoryBusinessGraph(),
                )
            else:
                self._resources = KnowledgeResources(
                    HTTPDocumentParser(s),
                    HTTPEmbedding(s),
                    HTTPReranker(s),
                    ElasticsearchIndex(s),
                    QdrantIndex(s),
                    Neo4jIndex(s),
                    Neo4jBusinessGraph(s),
                )
        return self._resources

    def for_snapshot(self, snapshot: dict) -> KnowledgeResourceManager:
        """Create an independently-owned bundle from immutable job settings."""
        values = {"knowledge_backend": snapshot.get("backend", self.settings.knowledge_backend)}
        validated = {}
        for capability, data in snapshot["capabilities"].items():
            if capability not in {"embedding", "rerank", "parser"}:
                continue
            config = ModelAliasInput.model_validate(
                {k: v for k, v in data.items() if k not in {"alias", "updated_at"}}
            )
            expected = (
                {"fake"}
                if self.settings.knowledge_backend == "fake"
                else {"custom_http", "local_http"}
            )
            if config.provider not in expected:
                raise ValueError("snapshot provider is not available in this runtime")
            if config.capability != capability:
                raise ValueError("snapshot capability mismatch")
            validated[capability] = config
            if capability == "embedding":
                values["embedding_model"] = config.model_name
                values["embedding_revision"] = config.model_snapshot or ""
                if "dimensions" in config.config:
                    values["embedding_dimensions"] = config.config["dimensions"]
            elif capability == "rerank":
                values["reranker_model"] = config.model_name
                values["reranker_revision"] = config.model_snapshot or ""
        settings = Settings.model_validate({**self.settings.model_dump(), **values})
        manager = KnowledgeResourceManager(settings)
        resources = manager.get()
        if settings.knowledge_backend == "fake":
            # In-memory stores belong to the application, not an individual attempt.
            shared = self.get()
            resources.fulltext, resources.vector, resources.graph = (
                shared.fulltext,
                shared.vector,
                shared.graph,
            )
            resources.parser = shared.parser
        for capability, config in validated.items():
            resource = getattr(resources, "reranker" if capability == "rerank" else capability)
            for name, value in config.config.items():
                if hasattr(resource, name):
                    setattr(resource, name, value)
        return manager

    @asynccontextmanager
    async def scoped(self, snapshot: dict):
        """按任务快照提供隔离的知识资源上下文。"""

        # Legacy persisted snapshots predate capability aliases. Honor their pinned
        # model names/revisions instead of silently substituting current settings.
        if "capabilities" not in snapshot:
            provider = "fake" if self.settings.knowledge_backend == "fake" else "custom_http"
            snapshot = {
                **snapshot,
                "capabilities": {
                    "embedding": {
                        "provider": provider,
                        "capability": "embedding",
                        "model_name": snapshot.get(
                            "embedding_model", self.settings.embedding_model
                        ),
                        "model_snapshot": snapshot.get(
                            "embedding_revision", self.settings.embedding_revision
                        ),
                        "config": {
                            "dimensions": snapshot.get(
                                "dimensions", self.settings.embedding_dimensions
                            )
                        },
                    },
                    "rerank": {
                        "provider": provider,
                        "capability": "rerank",
                        "model_name": snapshot.get("reranker_model", self.settings.reranker_model),
                        "model_snapshot": snapshot.get(
                            "reranker_revision", self.settings.reranker_revision
                        ),
                    },
                },
            }
        manager = self.for_snapshot(snapshot)
        try:
            yield manager.get()
        finally:
            await manager.close()

    async def close(self):
        """关闭已创建的共享知识资源。"""

        if self._resources is not None:
            await self._resources.close()
            self._resources = None

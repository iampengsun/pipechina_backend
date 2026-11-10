"""Vendor-independent contracts. Scopes are computed by trusted application code."""

from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeScope(BaseModel):
    """限定一次检索允许访问的知识版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    # Explicit allow-list of immutable version IDs; empty means deny all.
    version_ids: list[UUID]
    generation: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    authorization_digest: str = Field(min_length=1)


class EvidenceRecord(BaseModel):
    """表示可索引和引用的最小证据记录。"""

    id: UUID
    version_id: UUID
    title: str
    text: str
    locator: dict = Field(default_factory=dict)
    vector: list[float] = Field(default_factory=list)


class SearchHit(BaseModel):
    """组合检索证据及其相关度得分。"""

    evidence: EvidenceRecord
    score: float


class ParsedBlock(BaseModel):
    """表示文档解析后的结构化内容块。"""

    id: str
    parent_id: str | None = None
    kind: Literal["heading", "paragraph", "table", "image", "list"]
    text: str
    page: int = Field(ge=1)
    locator: dict = Field(default_factory=dict)


class ParseStatus(BaseModel):
    """表示异步文档解析任务的当前状态。"""

    id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    blocks: list[ParsedBlock] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    parser_version: str


class RankScore(BaseModel):
    """记录重排后原始候选的位置和得分。"""

    index: int = Field(ge=0)
    score: float


class GraphFact(BaseModel):
    """表示带来源证据的知识图谱关系。"""

    id: UUID
    source: str
    relation: str
    target: str
    evidence_id: UUID
    version_id: UUID


class Closeable(Protocol):
    """约束外部资源提供异步关闭能力。"""

    async def close(self) -> None: ...


class DocumentParser(Closeable, Protocol):
    """定义文档提交和解析状态查询接口。"""

    async def submit(self, path: Path, *, request_id: str, mime_type: str) -> ParseStatus: ...
    async def status(self, task_id: str) -> ParseStatus: ...


class EmbeddingProvider(Closeable, Protocol):
    """定义文本向量化接口。"""

    name: str
    model: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class RerankProvider(Closeable, Protocol):
    """定义检索候选重排接口。"""

    name: str
    model: str

    async def rerank(self, query: str, texts: list[str]) -> list[RankScore]: ...


class FullTextIndex(Closeable, Protocol):
    """定义全文索引的生命周期和查询接口。"""

    async def initialize(self, generation: str) -> None: ...
    async def upsert(self, generation: str, records: list[EvidenceRecord]) -> None: ...
    async def search(
        self, query: str, scope: KnowledgeScope, limit: int = 20
    ) -> list[SearchHit]: ...
    async def delete_version(self, generation: str, version_id: UUID) -> None: ...


class VectorIndex(Closeable, Protocol):
    """定义向量索引的生命周期和查询接口。"""

    async def initialize(self, generation: str) -> None: ...
    async def upsert(self, generation: str, records: list[EvidenceRecord]) -> None: ...
    async def search(
        self, vector: list[float], scope: KnowledgeScope, limit: int = 20
    ) -> list[SearchHit]: ...
    async def delete_version(self, generation: str, version_id: UUID) -> None: ...


class GraphIndex(Closeable, Protocol):
    """定义图关系索引的生命周期和邻接查询接口。"""

    async def initialize(self, generation: str) -> None: ...
    async def upsert(self, generation: str, facts: list[GraphFact]) -> None: ...
    async def neighbors(
        self, entity: str, scope: KnowledgeScope, limit: int = 20
    ) -> list[GraphFact]: ...
    async def delete_version(self, generation: str, version_id: UUID) -> None: ...

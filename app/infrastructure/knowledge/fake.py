"""Deterministic in-memory ports for contract tests, not production retrieval."""

import asyncio
import hashlib
import math
from pathlib import Path
from uuid import UUID, uuid4

from app.ports.knowledge import (
    EvidenceRecord,
    GraphFact,
    KnowledgeScope,
    ParsedBlock,
    ParseStatus,
    RankScore,
    SearchHit,
)


class FakeModels:
    name = "fake"
    model = "fake-knowledge-v1"

    def __init__(self, dimensions: int):
        self.dimensions = dimensions
        self.parses: dict[str, ParseStatus] = {}
        self.requests: dict[str, str] = {}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [
                (hashlib.sha256(text.encode()).digest()[i % 32] - 127) / 128
                for i in range(self.dimensions)
            ]
            for text in texts
        ]

    async def rerank(self, query: str, texts: list[str]) -> list[RankScore]:
        return sorted(
            [RankScore(index=i, score=float(query in text)) for i, text in enumerate(texts)],
            key=lambda x: x.score,
            reverse=True,
        )

    async def submit(self, path: Path, *, request_id: str, mime_type: str) -> ParseStatus:
        if request_id in self.requests:
            return self.parses[self.requests[request_id]]
        if mime_type not in {"text/plain", "text/markdown"}:
            raise ValueError("fake parser accepts text fixtures only")
        task_id = str(uuid4())
        result = ParseStatus(
            id=task_id,
            status="succeeded",
            parser_version="fake-v1",
            blocks=[
                ParsedBlock(
                    id="1", kind="paragraph", text=await asyncio.to_thread(path.read_text), page=1
                )
            ],
        )
        self.parses[task_id] = result
        self.requests[request_id] = task_id
        return result

    async def status(self, task_id: str) -> ParseStatus:
        return self.parses[task_id]

    async def close(self):
        pass


class FakeIndex:
    def __init__(self):
        self.records: dict[tuple[str, UUID], EvidenceRecord] = {}

    async def initialize(self, generation: str) -> None:
        pass

    async def upsert(self, generation: str, records: list[EvidenceRecord]) -> None:
        for record in records:
            self.records[generation, record.id] = record.model_copy(deep=True)

    async def search(
        self, query: str | list[float], scope: KnowledgeScope, limit: int = 20
    ) -> list[SearchHit]:
        hits = []
        for (generation, _), record in self.records.items():
            if generation != scope.generation or record.version_id not in scope.version_ids:
                continue
            if isinstance(query, str):
                score = float(query in record.text or query in record.title)
            else:
                if len(query) != len(record.vector):
                    raise ValueError("embedding dimension mismatch")
                denominator = math.sqrt(
                    sum(x * x for x in query) * sum(x * x for x in record.vector)
                )
                score = (
                    sum(x * y for x, y in zip(query, record.vector, strict=True)) / denominator
                    if denominator
                    else 0
                )
            hits.append(SearchHit(evidence=record.model_copy(deep=True), score=score))
        return sorted(hits, key=lambda x: x.score, reverse=True)[: max(1, min(limit, 100))]

    async def delete_version(self, generation: str, version_id: UUID) -> None:
        self.records = {
            key: row
            for key, row in self.records.items()
            if not (key[0] == generation and row.version_id == version_id)
        }

    async def close(self):
        pass


class FakeGraph:
    def __init__(self):
        self.facts: dict[tuple[str, UUID], GraphFact] = {}

    async def initialize(self, generation: str) -> None:
        pass

    async def upsert(self, generation: str, facts: list[GraphFact]) -> None:
        for fact in facts:
            self.facts[generation, fact.id] = fact.model_copy(deep=True)

    async def neighbors(
        self, entity: str, scope: KnowledgeScope, limit: int = 20
    ) -> list[GraphFact]:
        return [
            f.model_copy(deep=True)
            for (g, _), f in self.facts.items()
            if g == scope.generation
            and f.version_id in scope.version_ids
            and entity in {f.source, f.target}
        ][: max(1, min(limit, 100))]

    async def delete_version(self, generation: str, version_id: UUID) -> None:
        self.facts = {
            key: row
            for key, row in self.facts.items()
            if not (key[0] == generation and row.version_id == version_id)
        }

    async def close(self):
        pass

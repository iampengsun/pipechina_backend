"""Business relationship projection; graph storage never grants source access."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class BusinessNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    kind: str
    record_id: str
    revision: str


class BusinessEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    source: str
    target: str
    relation: str
    reference: str


class BusinessPath(BaseModel):
    nodes: list[str]
    edges: list[str]


class BusinessGraph(Protocol):
    async def replace(
        self, generation: str, nodes: list[BusinessNode], edges: list[BusinessEdge]
    ) -> None: ...
    async def paths(
        self,
        generation: str,
        root: str,
        allowed: list[str],
        depth: int,
        limit: int,
        *,
        dependencies_only: bool = False,
    ) -> list[BusinessPath]: ...
    async def delete_other_generations(self, generation: str) -> None: ...
    async def close(self) -> None: ...

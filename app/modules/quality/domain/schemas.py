from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MetricTarget(Input):
    operator: Literal["gte", "lte"]
    value: float = Field(allow_inf_nan=False)


class ImprovementCreate(Input):
    kind: Literal["rag", "agent"]
    baseline_id: UUID
    title: str = Field(min_length=2, max_length=200)
    problem: str = Field(min_length=2, max_length=4000)
    measures: str = Field(min_length=2, max_length=4000)
    assignee_id: UUID | None = None
    reviewer_id: UUID | None = None
    due_at: AwareDatetime | None = None
    targets: dict[str, MetricTarget] = Field(min_length=1, max_length=20)


class ImplementInput(Input):
    expected_version: int = Field(ge=1)
    changes: str = Field(min_length=2, max_length=4000)
    revision: str = Field(min_length=2, max_length=500)


class RetestInput(Input):
    expected_version: int = Field(ge=1)
    evaluation_id: UUID


class AcceptInput(Input):
    expected_version: int = Field(ge=1)
    approved: bool
    reason: str = Field(min_length=2, max_length=4000)


class AssignInput(Input):
    expected_version: int = Field(ge=1)
    assignee_id: UUID
    reviewer_id: UUID
    due_at: AwareDatetime
    reason: str = Field(min_length=2, max_length=2000)


class ImprovementView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: Literal["rag", "agent"]
    title: str
    problem: str
    measures: str
    baseline_id: UUID
    baseline: dict
    targets: dict[str, MetricTarget]
    status: Literal["confirmed", "implemented", "retested", "closed"]
    version: int
    history: list[dict]
    created_by: UUID
    assignee_id: UUID | None
    reviewer_id: UUID | None
    due_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

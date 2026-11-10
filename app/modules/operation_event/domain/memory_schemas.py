from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.ports.models import StrictProviderOutput


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ObservationCreate(Input):
    expected_version_id: UUID
    content: str = Field(min_length=2, max_length=4000)


class ObservationReview(Input):
    expected_version: int = Field(ge=1)
    action: Literal["confirm", "retract"]
    reason: str = Field(min_length=2, max_length=2000)


class ConsolidationInput(Input):
    expected_version: int = Field(ge=1)
    expires_at: AwareDatetime | None = None


class ExperienceDraft(StrictProviderOutput):
    title: str = Field(min_length=2, max_length=200)
    equipment_models: list[str] = Field(max_length=20)
    symptom: str = Field(min_length=2, max_length=500)
    preconditions: list[str] = Field(max_length=20)
    actions: list[str] = Field(min_length=1, max_length=30)
    outcome: str = Field(min_length=2, max_length=3000)
    cautions: list[str] = Field(max_length=20)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)


class MemoryCritique(StrictProviderOutput):
    supported: bool
    issues: list[str] = Field(max_length=20)
    evidence_ids: list[str] = Field(max_length=100)


class ExperienceReview(Input):
    expected_version: int = Field(ge=1)
    approved: bool
    reason: str = Field(min_length=2, max_length=2000)
    supersedes_ids: list[UUID] = Field(default_factory=list, max_length=20)


class ExperienceRevision(Input):
    expected_version: int = Field(ge=1)
    content: ExperienceDraft
    expires_at: AwareDatetime
    reason: str = Field(min_length=2, max_length=2000)


class ExperienceWithdrawal(Input):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=2000)


class ExperienceMerge(Input):
    expected_versions: dict[UUID, int] = Field(min_length=2, max_length=10)
    expires_at: AwareDatetime | None = None
    reason: str = Field(min_length=2, max_length=2000)


class ExperienceApplicability(StrictProviderOutput):
    applicable_evidence_ids: list[str]
    excluded_evidence_ids: list[str]
    differences: list[str]
    unknowns: list[str]


class ObservationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    event_id: UUID
    event_version_id: UUID
    content: str
    status: Literal["pending", "confirmed", "retracted"]
    version: int
    created_by: UUID
    confirmed_by: UUID | None
    reason: str | None
    created_at: AwareDatetime


class CaseMemoryView(BaseModel):
    event_id: UUID
    event_version_id: UUID
    observation_revision: str
    summaries: list[dict]
    observations: list[ObservationView]
    instruction: str


class ExperienceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    status: Literal["pending", "approved", "rejected", "withdrawn", "superseded"]
    version: int
    content: ExperienceDraft
    source_orders: list[dict]
    dependency_ids: list[UUID]
    supersedes_ids: list[UUID]
    checks: dict
    expires_at: AwareDatetime
    created_by: UUID
    reviewed_by: UUID | None
    review_reason: str | None
    parent_id: UUID | None
    job_id: UUID | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ConsolidationView(BaseModel):
    job_id: UUID | None
    id: UUID | None = None

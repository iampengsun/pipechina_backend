from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.shared.measurements import RetestMeasurement


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HazardCreate(Input):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1)
    location: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=80)
    source_type: Literal["manual", "inspection_finding", "production_event", "meeting", "handover"]
    source_id: UUID | None = None
    organization_unit_id: UUID | None = None


class HazardAssessmentInput(Input):
    likelihood: int = Field(ge=1, le=5)
    consequence: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1)
    control_measures: str = Field(min_length=1)
    assignee_id: UUID
    due_at: AwareDatetime
    expected_version: int = Field(ge=1)


class HazardStartInput(Input):
    reason: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class HazardRectificationInput(Input):
    measurements: list[RetestMeasurement] = Field(min_length=1, max_length=30)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)
    action: str = Field(min_length=1)
    result: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class HazardAcceptanceInput(Input):
    rectification_id: UUID
    measurements: list[RetestMeasurement] = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class HazardWorkOrderLinkInput(Input):
    work_order_id: UUID


class HazardView(BaseModel):
    id: UUID
    organization_unit_id: UUID
    hazard_no: str
    title: str
    description: str
    location: str
    category: str
    source_type: str
    source_id: UUID | None
    status: str
    risk_level: str | None
    likelihood: int | None
    consequence: int | None
    risk_score: int | None
    control_measures: str | None
    assignee_id: UUID | None
    due_at: datetime | None
    version: int
    created_by: UUID
    closed_by: UUID | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    overdue: bool = False
    model_config = ConfigDict(from_attributes=True)

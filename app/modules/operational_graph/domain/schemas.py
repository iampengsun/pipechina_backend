from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

RecordKind = Literal["event", "hazard", "work_order"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StationCreate(Input):
    organization_unit_id: UUID | None = None
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)


class EquipmentCreate(Input):
    station_id: UUID
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    equipment_type: str = Field(min_length=1, max_length=100)


class EquipmentUpdate(Input):
    expected_version: int = Field(ge=1)
    active: bool
    reason: str = Field(min_length=2, max_length=2000)


class AliasCreate(Input):
    expected_version: int = Field(ge=1)
    alias: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=2, max_length=2000)


FailureModeCreate = StationCreate


class AssociationCreate(Input):
    equipment_id: UUID
    failure_mode_id: UUID
    record_kind: RecordKind
    record_id: UUID
    source_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=2, max_length=2000)


class AssociationUpdate(Input):
    expected_version: int = Field(ge=1)
    active: bool
    source_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=2, max_length=2000)


class ImpactDecision(Input):
    expected_version: int = Field(ge=1)
    target_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["not_applicable", "reassessment_required", "revalidated", "retired"]
    reason: str = Field(min_length=2, max_length=4000)
    verification_id: UUID | None = None


class StationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    organization_unit_id: UUID
    code: str
    name: str
    created_by: UUID
    created_at: datetime


class EquipmentView(StationView):
    station_id: UUID
    equipment_type: str
    active: bool
    version: int


class AssociationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    equipment_id: UUID
    failure_mode_id: UUID
    record_kind: RecordKind
    record_id: UUID
    source_revision: str
    active: bool
    version: int
    reason: str
    confirmed_by: UUID
    created_at: datetime


class ProjectionView(BaseModel):
    generation: str
    synced_at: str | None
    source_changes_pending: bool
    limited: bool
    complete: bool


class RecordView(BaseModel):
    key: str
    kind: str
    record_id: str
    revision: str
    title: str
    state: str
    binding_revision: str | None
    details: dict


class TraceView(BaseModel):
    root: str
    nodes: list[RecordView]
    edges: list[dict]
    paths: list[dict]
    validation: dict
    projection: ProjectionView
    interpretation: str


class ImpactView(BaseModel):
    source: RecordView
    change: dict | None
    targets: list[dict]
    validation: dict
    projection: ProjectionView
    interpretation: str


class RecurrenceView(BaseModel):
    equipment_id: UUID
    failure_mode_id: UUID
    episodes: list[dict]
    visible_episode_count: int
    repeated_observations: bool
    stale_associations: bool
    validation: dict
    projection: ProjectionView
    interpretation: str


class AliasWithdrawal(Input):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=2000)


class StationLocationInput(Input):
    expected_version: int = Field(ge=0)
    coordinate_system: Literal["WGS84"] = "WGS84"
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    active: bool = True
    address: str = Field(default="", max_length=1000)
    source: str = Field(min_length=2, max_length=500)
    reason: str = Field(min_length=2, max_length=2000)

    @model_validator(mode="after")
    def coordinates(self):
        if self.active and (self.latitude is None or self.longitude is None):
            raise ValueError("active locations require confirmed WGS84 latitude and longitude")
        if not self.active and (self.latitude is not None or self.longitude is not None):
            raise ValueError("withdrawn locations must omit coordinates")
        return self


class StationLocationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    station_id: UUID
    version: int
    latitude: float | None
    longitude: float | None
    coordinate_system: Literal["WGS84"] = "WGS84"
    active: bool
    address: str
    source: str
    reason: str
    created_by: UUID
    created_at: datetime

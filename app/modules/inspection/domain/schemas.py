from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class InspectionCreate(BaseModel):
    station_name: str = Field(min_length=2, max_length=200)
    pipeline_name: str | None = Field(default=None, max_length=200)
    equipment_name: str | None = Field(default=None, max_length=200)
    inspected_at: datetime
    notes: str | None = Field(default=None, max_length=2000)


class InspectionView(BaseModel):
    id: UUID
    organization_unit_id: UUID
    station_name: str
    pipeline_name: str | None
    equipment_name: str | None
    inspected_at: datetime
    notes: str | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class InspectionImageCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=3, max_length=120)
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class ImageComplete(BaseModel):
    checksum: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class FindingReview(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class FindingLinkEvent(BaseModel):
    event_id: UUID


class RevokeLinkInput(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)

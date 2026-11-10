from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ReportCreate(BaseModel):
    report_type: Literal["daily", "incident_review"]
    business_date: date
    timezone: str = "Asia/Shanghai"
    scope_filter: dict[str, Any] = Field(default_factory=dict)


class ReportView(BaseModel):
    id: UUID
    report_type: str
    business_date: date
    organization_unit_id: UUID
    timezone: str
    scope_filter: dict
    status: str
    current_version_id: UUID | None
    published_version_id: UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReportContentUpdate(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    content: dict[str, Any]


class ReportReviewInput(BaseModel):
    approved: bool
    reason: str = Field(min_length=2, max_length=1000)


class ReportWithdrawInput(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class ReportExportCreate(BaseModel):
    format: Literal["docx", "pdf"] = "docx"

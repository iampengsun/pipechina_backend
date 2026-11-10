from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.shared.measurements import RetestMeasurement


class WorkOrderView(BaseModel):
    id: UUID
    organization_unit_id: UUID
    event_id: UUID | None
    order_no: str
    title: str
    description: str
    risk_level: str
    status: str
    version: int
    assignee_id: UUID | None
    due_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkOrderReviewInput(BaseModel):
    approved: bool
    reason: str = Field(min_length=2, max_length=1000)
    expected_version: int


class WorkOrderTransitionInput(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)
    expected_version: int


class DispatchInput(WorkOrderTransitionInput):
    assignee_id: UUID
    due_at: AwareDatetime | None = None


class WorkOrderResolutionInput(WorkOrderTransitionInput):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=2, max_length=4000)
    result: str = Field(min_length=2, max_length=4000)
    measurements: list[RetestMeasurement] = Field(min_length=1, max_length=30)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)


class WorkOrderAcceptanceInput(WorkOrderTransitionInput):
    model_config = ConfigDict(extra="forbid")
    resolution_id: UUID
    approved: bool = True
    measurements: list[RetestMeasurement] = Field(min_length=1, max_length=30)


class ReminderAcknowledgeInput(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=2000)


class WorkOrderAcceptanceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    work_order_id: UUID
    resolution_id: UUID
    approved: bool
    measurements: list[RetestMeasurement]
    reason: str
    reviewed_by: UUID
    created_at: datetime
    updated_at: datetime


class WorkOrderResolutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    work_order_id: UUID
    order_version: int
    action: str
    result: str
    measurements: list[RetestMeasurement]
    attachment_ids: list[UUID]
    submitted_by: UUID
    created_at: datetime
    updated_at: datetime
    acceptance: WorkOrderAcceptanceView | None = None


class WorkOrderReminderView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    work_order_id: UUID
    reminder_type: str
    scheduled_at: datetime
    status: str
    sent_at: datetime | None
    recipient_id: UUID | None
    version: int
    acknowledged_by: UUID | None
    acknowledged_at: datetime | None
    acknowledge_reason: str | None
    payload: dict
    created_at: datetime
    updated_at: datetime

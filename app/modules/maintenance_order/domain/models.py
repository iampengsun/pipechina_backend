from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.model_mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.types import JSON_DOCUMENT


class AbnormalityAssessment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "abnormality_assessments"
    __table_args__ = (Index("ix_assessment_event_status", "event_id", "review_status"),)

    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("production_events.id", ondelete="RESTRICT")
    )
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    reviewed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    review_reason: Mapped[str | None] = mapped_column(Text)


class WorkOrder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "work_orders"
    __table_args__ = (
        UniqueConstraint("source_workflow_run_id", name="uq_work_order_workflow"),
        Index("ix_work_orders_org_status", "organization_unit_id", "status"),
    )

    organization_unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("production_events.id", ondelete="RESTRICT")
    )
    source_workflow_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="RESTRICT")
    )
    order_no: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    assignee_id: Mapped[UUID | None] = mapped_column(ForeignKey("user_accounts.id"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class WorkOrderTransition(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "work_order_transitions"
    __table_args__ = (Index("ix_work_order_transition_time", "work_order_id", "occurred_at"),)

    work_order_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_orders.id", ondelete="RESTRICT")
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version_after: Mapped[int] = mapped_column(Integer, nullable=False)


class WorkOrderAttachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "work_order_attachments"

    work_order_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_orders.id", ondelete="RESTRICT"), index=True
    )
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class WorkOrderReminder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "work_order_reminders"
    __table_args__ = (
        UniqueConstraint(
            "work_order_id", "reminder_type", "scheduled_at", name="uq_work_order_reminder"
        ),
    )

    work_order_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_orders.id", ondelete="RESTRICT"), index=True
    )
    reminder_type: Mapped[str] = mapped_column(String(40), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipient_id: Mapped[UUID | None] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    acknowledged_by: Mapped[UUID | None] = mapped_column(ForeignKey("user_accounts.id"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledge_reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)


class WorkOrderResolution(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "work_order_resolutions"
    __table_args__ = (
        UniqueConstraint("work_order_id", "order_version", name="uq_order_resolution_version"),
    )
    work_order_id: Mapped[UUID] = mapped_column(ForeignKey("work_orders.id"), index=True)
    order_version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    measurements: Mapped[list] = mapped_column(JSON_DOCUMENT)
    attachment_ids: Mapped[list] = mapped_column(JSON_DOCUMENT)
    submitted_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))


class WorkOrderAcceptance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "work_order_acceptances"
    resolution_id: Mapped[UUID] = mapped_column(
        ForeignKey("work_order_resolutions.id"), unique=True
    )
    work_order_id: Mapped[UUID] = mapped_column(ForeignKey("work_orders.id"), index=True)
    approved: Mapped[bool] = mapped_column()
    measurements: Mapped[list] = mapped_column(JSON_DOCUMENT)
    reason: Mapped[str] = mapped_column(Text)
    reviewed_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))

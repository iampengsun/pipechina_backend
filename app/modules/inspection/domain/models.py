from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
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


class InspectionRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inspection_records"
    __table_args__ = (Index("ix_inspection_org_time", "organization_unit_id", "inspected_at"),)

    organization_unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    station_name: Mapped[str] = mapped_column(String(200), nullable=False)
    pipeline_name: Mapped[str | None] = mapped_column(String(200))
    equipment_name: Mapped[str | None] = mapped_column(String(200))
    inspected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class InspectionImage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inspection_images"
    __table_args__ = (Index("ix_inspection_images_record_status", "inspection_id", "status"),)

    inspection_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inspection_records.id", ondelete="RESTRICT")
    )
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    protected_reason: Mapped[str | None] = mapped_column(String(300))


class InspectionFinding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inspection_findings"
    __table_args__ = (Index("ix_findings_inspection_status", "inspection_id", "review_status"),)

    inspection_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inspection_records.id", ondelete="RESTRICT")
    )
    image_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inspection_images.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    reviewed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    review_reason: Mapped[str | None] = mapped_column(Text)
    workflow_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("production_events.id", ondelete="RESTRICT"), unique=True
    )


class InspectionFindingLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inspection_finding_links"
    __table_args__ = (
        UniqueConstraint(
            "finding_id", "target_type", "target_id", "active", name="uq_finding_link"
        ),
    )

    finding_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inspection_findings.id", ondelete="RESTRICT")
    )
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revoked_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)

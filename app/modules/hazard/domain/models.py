from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
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


class Hazard(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "hazards"
    __table_args__ = (
        Index("ix_hazards_org_status_due", "organization_unit_id", "status", "due_at"),
        Index("ix_hazards_source", "source_type", "source_id"),
        CheckConstraint(
            "status IN ('pending_assessment','pending_rectification','rectifying',"
            "'pending_acceptance','closed','cancelled')",
            name="hazard_status",
        ),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low','medium','high','major')",
            name="hazard_risk_level",
        ),
        CheckConstraint(
            "likelihood IS NULL OR likelihood BETWEEN 1 AND 5",
            name="hazard_likelihood_range",
        ),
        CheckConstraint(
            "consequence IS NULL OR consequence BETWEEN 1 AND 5",
            name="hazard_consequence_range",
        ),
        CheckConstraint("version >= 1", name="hazard_version_positive"),
    )

    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organization_units.id"), nullable=False
    )
    hazard_no: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), default="pending_assessment", nullable=False)
    risk_level: Mapped[str | None] = mapped_column(String(20))
    likelihood: Mapped[int | None] = mapped_column(Integer)
    consequence: Mapped[int | None] = mapped_column(Integer)
    risk_score: Mapped[int | None] = mapped_column(Integer)
    control_measures: Mapped[str | None] = mapped_column(Text)
    assignee_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id")
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id"), nullable=False
    )
    closed_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HazardAssessment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "hazard_assessments"
    __table_args__ = (
        UniqueConstraint("hazard_id", "version", name="uq_hazard_assessment_version"),
        CheckConstraint("likelihood BETWEEN 1 AND 5", name="assessment_likelihood_range"),
        CheckConstraint("consequence BETWEEN 1 AND 5", name="assessment_consequence_range"),
    )

    hazard_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("hazards.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    likelihood: Mapped[int] = mapped_column(Integer, nullable=False)
    consequence: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    assessed_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id"), nullable=False
    )


class HazardRectification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    hazard_version: Mapped[int | None] = mapped_column(Integer)
    measurements: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    attachment_ids: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    __tablename__ = "hazard_rectifications"
    __table_args__ = (Index("ix_hazard_rectification_time", "hazard_id", "created_at"),)

    hazard_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("hazards.id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id"), nullable=False
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HazardAcceptance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    rectification_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("hazard_rectifications.id"), unique=True
    )
    measurements: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    __tablename__ = "hazard_acceptances"
    __table_args__ = (Index("ix_hazard_acceptance_time", "hazard_id", "created_at"),)

    hazard_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("hazards.id", ondelete="RESTRICT"), index=True
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id"), nullable=False
    )
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HazardTransition(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "hazard_transitions"
    __table_args__ = (Index("ix_hazard_transition_time", "hazard_id", "occurred_at"),)

    hazard_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("hazards.id", ondelete="RESTRICT")
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version_after: Mapped[int] = mapped_column(Integer, nullable=False)


class HazardWorkOrderLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "hazard_work_order_links"
    __table_args__ = (
        UniqueConstraint("hazard_id", "work_order_id", name="uq_hazard_work_order_link"),
    )

    hazard_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("hazards.id", ondelete="RESTRICT"), index=True
    )
    work_order_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_orders.id", ondelete="RESTRICT"), index=True
    )
    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id"), nullable=False
    )


class HazardAttachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "hazard_attachments"
    hazard_id: Mapped[UUID] = mapped_column(ForeignKey("hazards.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))

"""Case-scoped working memory and reviewed experience with explicit provenance."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.model_mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.types import JSON_DOCUMENT


class CaseObservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "case_observations"
    __table_args__ = (
        CheckConstraint("status IN ('pending','confirmed','retracted')", name="observation_status"),
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("production_events.id"), index=True)
    event_version_id: Mapped[UUID] = mapped_column(ForeignKey("production_event_versions.id"))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))
    confirmed_by: Mapped[UUID | None] = mapped_column(ForeignKey("user_accounts.id"))
    reason: Mapped[str | None] = mapped_column(Text)


class CaseMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "case_memories"
    __table_args__ = (UniqueConstraint("run_id", name="uq_case_memory_run"),)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("production_events.id"), index=True)
    event_version_id: Mapped[UUID] = mapped_column(ForeignKey("production_event_versions.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    observation_revision: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON_DOCUMENT)


class ExperienceMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "experience_memories"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','withdrawn','superseded')",
            name="experience_status",
        ),
    )
    organization_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization_units.id"), index=True
    )
    primary_order_id: Mapped[UUID] = mapped_column(ForeignKey("work_orders.id"), index=True)
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("async_jobs.id"), unique=True)
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("experience_memories.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    search_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    applicability_key: Mapped[str] = mapped_column(String(64), index=True)
    source_orders: Mapped[list] = mapped_column(JSON_DOCUMENT)
    dependency_ids: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    supersedes_ids: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    checks: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))
    reviewed_by: Mapped[UUID | None] = mapped_column(ForeignKey("user_accounts.id"))
    review_reason: Mapped[str | None] = mapped_column(Text)

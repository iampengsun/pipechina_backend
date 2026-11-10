"""Persist human-confirmed evaluation improvements and immutable review history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.model_mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.types import JSON_DOCUMENT


class EvaluationImprovement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_improvements"
    __table_args__ = (
        CheckConstraint("kind IN ('rag','agent')", name="improvement_kind"),
        CheckConstraint(
            "status IN ('confirmed','implemented','retested','closed')",
            name="improvement_status",
        ),
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    assignee_id: Mapped[UUID | None] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    reviewer_id: Mapped[UUID | None] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    problem: Mapped[str] = mapped_column(Text)
    measures: Mapped[str] = mapped_column(Text)
    baseline_id: Mapped[UUID] = mapped_column(index=True)
    baseline: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    targets: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    status: Mapped[str] = mapped_column(String(24), default="confirmed")
    version: Mapped[int] = mapped_column(Integer, default=1)
    history: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)

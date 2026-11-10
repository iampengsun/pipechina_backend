from __future__ import annotations

from datetime import date, datetime
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
from app.shared.types import JSON_DOCUMENT


class OperationReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "operation_reports"
    __table_args__ = (
        UniqueConstraint(
            "report_type",
            "business_date",
            "organization_unit_id",
            "scope_digest",
            name="uq_operation_report_scope",
        ),
        Index("ix_operation_reports_status", "organization_unit_id", "status", "business_date"),
    )

    report_type: Mapped[str] = mapped_column(String(40), nullable=False)
    business_date: Mapped[date] = mapped_column(nullable=False)
    organization_unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai", nullable=False)
    scope_filter: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    published_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class ReportVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "report_versions"
    __table_args__ = (UniqueConstraint("report_id", "version", name="uq_report_version"),)

    report_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("operation_reports.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    reviewed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    review_reason: Mapped[str | None] = mapped_column(Text)
    immutable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class ReportSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "report_sources"
    __table_args__ = (
        UniqueConstraint("report_version_id", "source_type", "source_id", name="uq_report_source"),
    )

    report_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("report_versions.id", ondelete="RESTRICT"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    snapshot: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)


class ReportExport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "report_exports"
    __table_args__ = (Index("ix_report_exports_report_status", "report_id", "status"),)

    report_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("operation_reports.id", ondelete="RESTRICT")
    )
    report_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("report_versions.id", ondelete="RESTRICT")
    )
    export_format: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(1024))
    error_code: Mapped[str | None] = mapped_column(String(100))
    requested_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

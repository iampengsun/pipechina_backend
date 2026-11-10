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


class Meeting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meetings"
    __table_args__ = (
        Index("ix_meetings_org_started", "organization_unit_id", "started_at"),
        CheckConstraint("size_bytes > 0", name="meeting_size_positive"),
        CheckConstraint("duration_ms IS NULL OR duration_ms > 0", name="meeting_duration_positive"),
        CheckConstraint(
            "upload_status IN ('pending','verified','failed','deleting','deleted')",
            name="meeting_upload_status",
        ),
        CheckConstraint(
            "status IN ('draft','transcribed','published','withdrawn')", name="meeting_status"
        ),
    )

    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organization_units.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    location: Mapped[str | None] = mapped_column(String(300))
    filename: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    mime_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    upload_status: Mapped[str] = mapped_column(String(32), default="pending")
    recording_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recording_delete_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    current_transcript_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "meeting_transcript_versions.id", use_alter=True, name="fk_meeting_current_transcript"
        ),
    )
    reader_ids: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    acl_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("user_accounts.id"))
    published_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id")
    )


class MeetingParticipant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meeting_participants"
    __table_args__ = (
        UniqueConstraint("meeting_id", "participant_key", name="uq_meeting_participant_key"),
    )
    meeting_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meetings.id"), index=True
    )
    participant_key: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(200))
    user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("user_accounts.id"))
    role: Mapped[str | None] = mapped_column(String(100))


class MeetingTranscriptVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meeting_transcript_versions"
    __table_args__ = (
        UniqueConstraint("meeting_id", "version", name="uq_meeting_transcript_version"),
        CheckConstraint("version >= 1", name="meeting_transcript_version_pos"),
        CheckConstraint("source IN ('ai','manual')", name="meeting_transcript_source"),
    )
    meeting_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meetings.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(32))
    full_text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(20))
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("user_accounts.id"))


class MeetingTranscriptSegment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meeting_transcript_segments"
    __table_args__ = (
        UniqueConstraint("transcript_version_id", "segment_index", name="uq_meeting_segment_index"),
        CheckConstraint("segment_index >= 0", name="meeting_segment_index_nonneg"),
        CheckConstraint("start_ms >= 0 AND end_ms > start_ms", name="meeting_segment_range"),
    )
    transcript_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meeting_transcript_versions.id"), index=True
    )
    segment_index: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    speaker_label: Mapped[str | None] = mapped_column(String(120))
    confidence: Mapped[float | None] = mapped_column()


class MeetingKnowledgePublication(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meeting_knowledge_publications"
    __table_args__ = (
        UniqueConstraint(
            "meeting_id", "transcript_version_id", name="uq_meeting_knowledge_version"
        ),
    )
    meeting_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meetings.id"), index=True
    )
    transcript_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meeting_transcript_versions.id")
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_bases.id")
    )
    knowledge_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_documents.id")
    )
    knowledge_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_versions.id")
    )
    index_job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("async_jobs.id")
    )
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("user_accounts.id"))


class MeetingMinutesVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """会议纪要草稿和人工确认版本，与来源逐字稿固定关联。"""

    __tablename__ = "meeting_minutes_versions"
    __table_args__ = (UniqueConstraint("meeting_id", "version", name="uq_meeting_minutes_version"),)

    meeting_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meetings.id", ondelete="RESTRICT"), index=True
    )
    transcript_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_transcript_versions.id", name="fk_meeting_minutes_transcript"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("user_accounts.id"))
    confirmed_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id")
    )

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


class KnowledgeBase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_bases"
    organization_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization_units.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))
    reader_ids: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    acl_version: Mapped[int] = mapped_column(Integer, default=1)


class KnowledgeDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("base_id", "code", name="uq_knowledge_document_code"),)
    base_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    code: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(40))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))
    reader_ids: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    acl_version: Mapped[int] = mapped_column(Integer, default=1)


class KnowledgeVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "number", name="uq_knowledge_version_number"),
        CheckConstraint("number > 0", name="knowledge_version_positive"),
        CheckConstraint(
            "status IN ('uploading','uploaded','processing','failed','review', "
            "'approved','rejected','published','withdrawn')",
            name="knowledge_version_status",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="knowledge_effective_range",
        ),
    )
    document_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="uploading")
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(150))
    size_bytes: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    equipment_models: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    authority: Mapped[str] = mapped_column(String(120), default="unclassified")
    generation: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    artifact: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    index_state: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))
    reviewed_by: Mapped[UUID | None] = mapped_column(ForeignKey("user_accounts.id"))
    review_reason: Mapped[str | None] = mapped_column(Text)


class KnowledgeChunk(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("version_id", "ordinal", name="uq_knowledge_chunk_order"),)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_versions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    parent_text: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)


class KnowledgeFact(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "knowledge_facts"
    version_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_versions.id"), index=True)
    evidence_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_chunks.id"))
    source: Mapped[str] = mapped_column(String(300))
    relation: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(300))
    evidence_quote: Mapped[str] = mapped_column(Text)


class KnowledgeConversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_conversations"
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))


class KnowledgeRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_runs"
    conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_conversations.id"))
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("async_jobs.id"))
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    answer: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    trace: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    next_sequence: Mapped[int] = mapped_column(Integer, default=1)


class KnowledgeRunEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_knowledge_run_sequence"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)


class KnowledgeFeedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_feedback"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_runs.id"), index=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))
    category: Mapped[str] = mapped_column(String(50))
    comment: Mapped[str] = mapped_column(Text)


class KnowledgeEvaluation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_evaluations"
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("async_jobs.id"))
    dataset: Mapped[dict] = mapped_column(JSON_DOCUMENT)
    options: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    results: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")

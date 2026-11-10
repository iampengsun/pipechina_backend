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


class ProductionEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存生产事件的当前检索字段、审核状态和生效版本指针。"""

    __tablename__ = "production_events"  # 对应的数据库表名
    __table_args__ = (  # 定义业务查询索引或唯一性约束
        Index("ix_production_events_org_time", "organization_unit_id", "occurred_at"),
        Index("ix_production_events_status", "business_status", "updated_at"),
    )

    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )  # 所属组织单元，用于数据范围隔离
    title: Mapped[str] = mapped_column(String(300), nullable=False)  # 事件或条目的标题
    event_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)  # 生产事件类型
    severity: Mapped[str] = mapped_column(String(20), nullable=False)  # 事件严重度
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # 确认后的发生时间，可为空
    business_status: Mapped[str] = mapped_column(
        String(32), default="candidate", nullable=False
    )  # 事件业务状态
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # 当前生效版本标识
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)  # 创建用户标识
    confirmed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # 人工确认用户标识


class ProductionEventVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存事件描述与结构化数据的历史版本，不保存主表字段的完整快照。"""

    __tablename__ = "production_event_versions"  # 对应的数据库表名
    __table_args__ = (
        UniqueConstraint("event_id", "version", name="uq_production_event_version"),
    )  # 定义业务查询索引或唯一性约束

    event_id: Mapped[UUID] = mapped_column(  # 关联生产事件标识
        Uuid(as_uuid=True), ForeignKey("production_events.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # 所属记录内的版本号
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # 版本生成方式
    description: Mapped[str] = mapped_column(Text, nullable=False)  # 该版本或条目的完整描述
    structured_data: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 扩展结构化内容
    confidence: Mapped[float | None] = mapped_column()  # 抽取置信度
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)  # 创建用户标识


class EventSourceLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """记录事件的来源对象、来源版本和证据文字，支持追溯。"""

    __tablename__ = "event_source_links"  # 对应的数据库表名
    __table_args__ = (  # 定义业务查询索引或唯一性约束
        UniqueConstraint("event_id", "source_type", "source_id", name="uq_event_source_link"),
    )

    event_id: Mapped[UUID] = mapped_column(  # 关联生产事件标识
        Uuid(as_uuid=True), ForeignKey("production_events.id", ondelete="RESTRICT"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(60), nullable=False)  # 来源对象类型
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)  # 来源主记录标识
    source_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # 固定来源版本标识
    evidence_text: Mapped[str | None] = mapped_column(Text)  # 支持该候选的证据文字
    evidence_locator: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 证据位置扩展信息

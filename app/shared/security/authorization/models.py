from __future__ import annotations

# RBAC持久化模型：权限目录、角色、角色权限、用户角色和数据范围。
from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.model_mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.types import JSON_DOCUMENT


class PermissionDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """记录权限代码、所属模块和风险等级。"""

    __tablename__ = "permission_definitions"

    code: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    module: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RoleDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """记录角色的基本信息和启用状态。"""

    __tablename__ = "role_definitions"

    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 为兼容V1.3迁移保留；实际授权关系使用role_permissions表。
    permissions: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    system_role: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RolePermission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """建立角色与权限之间的多对多关联。"""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_pair"),
    )

    role_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("role_definitions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    permission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("permission_definitions.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )


class RoleAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """记录用户的角色授权、数据范围和有效期。"""

    __tablename__ = "role_assignments"
    __table_args__ = (
        Index("ix_role_assignments_user_active", "user_id", "active"),
        Index("ix_role_assignments_validity", "active", "effective_from", "expires_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    role_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("role_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope_type: Mapped[str] = mapped_column(String(32), default="own_org", nullable=False)
    data_scope: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 本行用户角色从什么时间点
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user_accounts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    grant_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user_accounts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

from __future__ import annotations

# 项目内部组织与用户模型；外部IdP只负责证明登录身份。
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.model_mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.types import JSON_DOCUMENT


class OrganizationUnit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """存储组织单元及其层级路径。"""

    __tablename__ = "organization_units"

    parent_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organization_units.id", ondelete="RESTRICT"),
        nullable=True,
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_type: Mapped[str] = mapped_column(String(32), default="department", nullable=False)
    path: Mapped[str] = mapped_column(String(1000), index=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class UserAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """存储外部身份对应的内部用户账号。"""

    __tablename__ = "user_accounts"
    __table_args__ = (
        UniqueConstraint(
            "external_issuer",
            "external_subject",
            name="uq_user_accounts_external_identity",
        ),
        Index("ix_user_accounts_organization_active", "organization_unit_id", "active"),
    )

    external_issuer: Mapped[str] = mapped_column(String(300), nullable=False)
    external_subject: Mapped[str] = mapped_column(String(200), nullable=False)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organization_units.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    authz_version: Mapped[int] = mapped_column(default=1, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)

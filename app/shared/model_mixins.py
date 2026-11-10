from datetime import datetime

# SQLAlchemy模型公共字段：UUID主键以及创建、更新时间。
from uuid import UUID

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.ids import new_id


class UUIDPrimaryKeyMixin:
    """为ORM模型提供UUID主键。"""

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id)


class TimestampMixin:
    """为ORM模型提供创建和更新时间。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),  # 使用包含时区信息的时间类型
        server_default=func.now(),  # 插入数据时由数据库生成当前时间
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),  # 插入数据时设置初始更新时间
        onupdate=func.now(),  # SQLAlchemy 更新数据时自动生成当前时间
        nullable=False,
    )

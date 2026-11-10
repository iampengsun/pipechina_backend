from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.operation_event.domain.models import ProductionEvent


class OperationEventRepository:
    """封装生产事件的数据库查询，不承担权限或状态校验。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存调用方数据库会话供仓储查询复用。"""
        self.session = session  # 复用调用方会话，不在仓储内提交事务

    async def get_event(self, event_id: UUID) -> ProductionEvent | None:
        """按事件标识读取主记录，不存在时返回空值。"""
        return await self.session.get(ProductionEvent, event_id)

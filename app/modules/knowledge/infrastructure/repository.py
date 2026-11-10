from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.domain.models import KnowledgeVersion


class KnowledgeRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def versions(self, document_id: UUID) -> list[KnowledgeVersion]:
        return list(
            await self.session.scalars(
                select(KnowledgeVersion)
                .where(KnowledgeVersion.document_id == document_id)
                .order_by(KnowledgeVersion.number)
            )
        )

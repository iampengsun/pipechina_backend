from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.meeting.domain.models import Meeting, MeetingTranscriptSegment


class MeetingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, meeting_id: UUID) -> Meeting | None:
        return await self.session.get(Meeting, meeting_id, populate_existing=True)

    async def list_segments(self, transcript_version_id: UUID) -> list[MeetingTranscriptSegment]:
        return list(
            await self.session.scalars(
                select(MeetingTranscriptSegment)
                .where(MeetingTranscriptSegment.transcript_version_id == transcript_version_id)
                .order_by(MeetingTranscriptSegment.segment_index)
            )
        )

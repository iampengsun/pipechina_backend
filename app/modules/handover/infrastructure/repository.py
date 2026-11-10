from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.handover.domain.models import AudioRecord, AudioTranscriptSegment


class HandoverRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_audio(self, audio_id: UUID) -> AudioRecord | None:
        # 根据音频id去AudioRecord表里面读取某行数据
        # select * from AudioRecord where audio_id = ?
        return await self.session.get(AudioRecord, audio_id)

    async def list_segments(self, version_id: UUID) -> list[AudioTranscriptSegment]:
        # 指定某一个音频的版本id，获取这个版本下所有的分段信息
        rows = await self.session.scalars(
            select(AudioTranscriptSegment)
            .where(AudioTranscriptSegment.transcript_version_id == version_id)
            .order_by(AudioTranscriptSegment.segment_index)
        )
        return list(rows)

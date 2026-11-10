from __future__ import annotations

# 视觉分析端口，屏蔽具体模型服务和部署方式。
from typing import Protocol

from app.ports.models import FindingCandidate, MediaRef


class VisionProvider(Protocol):
    """与推理运行时无关的巡检图片分析接口。"""

    name: str
    model: str

    async def inspect(
        self,
        media: MediaRef,
        *,
        context: str,
    ) -> list[FindingCandidate]: ...

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class EventExtractionCreate(BaseModel):
    """校验事件抽取的来源类型、来源标识和临时文字。"""

    source_type: str = Field(
        pattern=r"^(audio_transcript|manual_operation|raw_text)$"
    )  # 来源对象类型，分别是音频转文字，手动记录，临时文本
    # Field和pattern起到了一个检验的效果，类似于枚举
    source_id: UUID | None = None  # 来源主记录标识
    source_version_id: UUID | None = None  # 固定来源版本标识
    raw_text: str | None = None  # 待抽取的临时文字


class EventUpdate(BaseModel):
    """校验人工修改的主记录字段和新版本内容。"""

    expected_version_id: UUID | None = None
    title: str = Field(min_length=2, max_length=300)  # 事件或条目的标题
    event_type: str = Field(min_length=2, max_length=80)  # 生产事件类型
    severity: str = Field(pattern=r"^(low|medium|high|critical)$")  # 事件严重度
    occurred_at: datetime | None = None  # 确认后的发生时间，可为空
    description: str = Field(min_length=2)  # 该版本或条目的完整描述
    structured_data: dict[str, Any] = Field(default_factory=dict)  # 扩展结构化内容


class EventView(BaseModel):
    """定义事件主记录的接口响应，支持从 ORM 属性读取字段。"""

    id: UUID  # 资源唯一标识
    organization_unit_id: UUID  # 所属组织单元，用于数据范围隔离
    title: str  # 事件或条目的标题
    event_type: str  # 生产事件类型
    severity: str  # 事件严重度
    occurred_at: datetime | None  # 确认后的发生时间，可为空
    business_status: str  # 事件业务状态
    current_version_id: UUID | None  # 当前生效版本标识
    created_at: datetime  # 记录创建时间

    model_config = {"from_attributes": True}  # 允许从 ORM 对象属性构造响应


class MergeEventsInput(BaseModel):
    """校验待合并事件列表及新事件标题。"""

    event_ids: list[UUID] = Field(min_length=2)  # 待合并事件标识，长度约束不负责去重
    title: str = Field(min_length=2, max_length=300)  # 事件或条目的标题


class SplitEventItem(BaseModel):
    """描述一条拆分后候选事件的输入内容。"""

    title: str  # 事件或条目的标题
    event_type: str  # 生产事件类型
    severity: str = "medium"  # 事件严重度
    description: str  # 该版本或条目的完整描述


class SplitEventInput(BaseModel):
    """校验拆分请求至少提供两个候选条目。"""

    items: list[SplitEventItem] = Field(min_length=2)  # 拆分后的候选条目


class RejectInput(BaseModel):
    expected_version_id: UUID | None = None
    """校验人工驳回原因，供审核接口写入审计日志。"""

    reason: str = Field(min_length=2, max_length=1000)  # 驳回原因，供审计保存


class EventReviewInput(BaseModel):
    expected_version_id: UUID

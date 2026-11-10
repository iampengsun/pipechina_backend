"""Persistence owned by production-event investigation."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.model_mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.types import JSON_DOCUMENT


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存一次事件调查的输入版本、执行状态、结果和用量汇总。"""

    __tablename__ = "agent_runs"  # 对应的数据库表名
    __table_args__ = (  # 定义业务查询索引或唯一性约束
        Index("ix_agent_runs_event_status", "event_id", "status"),
        Index("ix_agent_runs_org_created", "organization_unit_id", "created_at"),
    )

    event_id: Mapped[UUID] = mapped_column(  # 关联生产事件标识
        Uuid(as_uuid=True), ForeignKey("production_events.id", ondelete="RESTRICT"), nullable=False
    )
    event_version_id: Mapped[UUID] = mapped_column(  # 本次调查固定的事件版本
        Uuid(as_uuid=True),
        ForeignKey("production_event_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_id: Mapped[UUID | None] = mapped_column(  # 关联异步任务标识
        Uuid(as_uuid=True), ForeignKey("async_jobs.id", ondelete="SET NULL"), unique=True
    )
    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )  # 所属组织单元，用于数据范围隔离
    status: Mapped[str] = mapped_column(
        String(32), default="queued", nullable=False
    )  # 当前执行状态
    current_stage: Mapped[str] = mapped_column(
        String(40), default="coordinator", nullable=False
    )  # 运行阶段标记
    config_snapshot: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 本次执行采用的配置快照
    result: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 本次运行的结果摘要
    error_detail: Mapped[str | None] = mapped_column(Text)  # 失败详情
    cancel_requested: Mapped[bool] = mapped_column(default=False, nullable=False)  # 是否已请求取消
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 开始执行时间
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 执行结束时间
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)  # 创建用户标识
    trace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)  # 整次调查的链路标识
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 模型输入用量
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 模型输出用量
    cost_microusd: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # 按配置价格折算的微美元成本


class AgentTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存调查中一个角色的目标、依赖、输出和执行用量。"""

    __tablename__ = "agent_tasks"  # 对应的数据库表名
    __table_args__ = (
        UniqueConstraint("run_id", "role", name="uq_agent_task_run_role"),
    )  # 定义业务查询索引或唯一性约束

    run_id: Mapped[UUID] = mapped_column(  # 所属调查运行标识
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(40), nullable=False)  # 角色名称
    goal: Mapped[str] = mapped_column(Text, nullable=False)  # 该角色的业务目标
    status: Mapped[str] = mapped_column(
        String(32), default="queued", nullable=False
    )  # 当前执行状态
    depends_on: Mapped[list] = mapped_column(
        JSON_DOCUMENT, default=list, nullable=False
    )  # 前置角色名称列表
    input_snapshot: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 角色输入快照字段
    output: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 角色的结构化输出
    error_detail: Mapped[str | None] = mapped_column(Text)  # 失败详情
    step_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # 已记录步骤数，也用于分配下一步骤序号
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 模型输入用量
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 模型输出用量
    cost_microusd: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # 按配置价格折算的微美元成本


class AgentStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """记录角色的一次模型或工具调用及其链路、耗时和用量。"""

    __tablename__ = "agent_steps"  # 对应的数据库表名
    __table_args__ = (
        UniqueConstraint("task_id", "step_index", name="uq_agent_step_index"),
    )  # 定义业务查询索引或唯一性约束

    task_id: Mapped[UUID] = mapped_column(  # 所属角色任务标识
        Uuid(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)  # 任务内的步骤序号
    action: Mapped[str] = mapped_column(String(80), nullable=False)  # 步骤动作或取证决策
    tool_name: Mapped[str | None] = mapped_column(String(120))  # 调用的工具或模型操作名称
    tool_arguments: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 工具调用参数
    observation: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 本次调用的可审计结果
    trace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)  # 整次调查的链路标识
    span_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)  # 单次调用链路标识
    parent_span_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # 可选的父调用标识
    duration_ms: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # 调用耗时，单位为毫秒
    input_tokens: Mapped[int | None] = mapped_column(Integer)  # 模型输入用量
    output_tokens: Mapped[int | None] = mapped_column(Integer)  # 模型输出用量
    cost_microusd: Mapped[int | None] = mapped_column(Integer)  # 按配置价格折算的微美元成本


class AgentEvidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存调查证据的内容快照、来源版本、哈希和访问范围。"""

    __tablename__ = "agent_evidence"  # 对应的数据库表名
    __table_args__ = (
        Index("ix_agent_evidence_run_source", "run_id", "source_server"),
    )  # 定义业务查询索引或唯一性约束

    run_id: Mapped[UUID] = mapped_column(  # 所属调查运行标识
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID] = mapped_column(  # 所属角色任务标识
        Uuid(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="CASCADE")
    )
    source_server: Mapped[str] = mapped_column(
        String(80), nullable=False
    )  # 证据来源标签，不表示 MCP 服务
    source_resource: Mapped[str] = mapped_column(String(200), nullable=False)  # 来源资源标识
    source_version: Mapped[str | None] = mapped_column(String(200))  # 证据对应的来源版本
    content: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 取证时保存的内容快照
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # 内容摘要，用于核对证据内容
    access_scope: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 取证时记录的访问范围


class AgentProposal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存调查形成的分类与处置建议，供后续人工审核。"""

    __tablename__ = "agent_proposals"  # 对应的数据库表名
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_agent_proposal_run"),
    )  # 定义业务查询索引或唯一性约束

    run_id: Mapped[UUID] = mapped_column(  # 所属调查运行标识
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False)  # 异常分类
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)  # 风险等级
    rationale: Mapped[str] = mapped_column(Text, nullable=False)  # 分类分级依据
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)  # 建议的处置措施
    evidence_ids: Mapped[list] = mapped_column(
        JSON_DOCUMENT, default=list, nullable=False
    )  # 引用的已保存证据标识
    unknowns: Mapped[list] = mapped_column(
        JSON_DOCUMENT, default=list, nullable=False
    )  # 尚未确定的事项
    conflicts: Mapped[list] = mapped_column(
        JSON_DOCUMENT, default=list, nullable=False
    )  # 复核发现的冲突
    review_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )  # 建议的人工审核状态


class AgentQualityEvaluation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存基础规则与 Critic 输出汇总的质量分，不代表事实准确率。"""

    __tablename__ = "agent_quality_evaluations"  # 对应的数据库表名
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_agent_quality_run"),
    )  # 定义业务查询索引或唯一性约束

    run_id: Mapped[UUID] = mapped_column(  # 所属调查运行标识
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    overall_score: Mapped[int] = mapped_column(Integer, nullable=False)  # 各质量维度的平均分
    evidence_coverage: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # 是否包含证据引用的规则分
    citation_validity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # 引用标识是否合法的规则分
    completeness: Mapped[int] = mapped_column(Integer, nullable=False)  # 关键字段是否齐全的规则分
    conflict_score: Mapped[int] = mapped_column(Integer, nullable=False)  # 按冲突数量扣减的规则分
    critic_score: Mapped[int] = mapped_column(Integer, nullable=False)  # 复核通过分
    model_judge_score: Mapped[int | None] = mapped_column(Integer)  # 同次 Critic 结论对应的分数
    details: Mapped[dict] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )  # 质量评分的补充统计


class AgentOfflineEvaluation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存专家标注与调查输出的离线对照快照，不参与业务审批。"""

    __tablename__ = "agent_offline_evaluations"

    requested_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_accounts.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    split: Mapped[str] = mapped_column(String(20), nullable=False)
    dataset: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    results: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)


class PromptOptimization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Development-only reflective search; publication requires a separate closed review."""

    __tablename__ = "prompt_optimizations"
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("async_jobs.id"), unique=True)
    evaluation_id: Mapped[UUID] = mapped_column(ForeignKey("agent_offline_evaluations.id"))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    options: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    results: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)
    candidate_id: Mapped[UUID | None] = mapped_column(ForeignKey("prompt_template_versions.id"))
    published_improvement_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("evaluation_improvements.id")
    )

from __future__ import annotations

# 平台公共表：上传会话、幂等、配置版本、异步任务、审计和事务Outbox。
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.model_mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.types import JSON_DOCUMENT


# 记录文件上传过程、完整性摘要和过期状态。
class UploadSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "upload_sessions"

    __table_args__ = (
        # 一个业务资源只能对应一个上传会话，避免完成上传时命中不确定记录。
        UniqueConstraint(
            "resource_type",
            "resource_id",
            name="uq_upload_sessions_resource",
        ),
        CheckConstraint("size_bytes >= 0", name="upload_size_nonnegative"),
        CheckConstraint(
            "status IN ('pending', 'uploaded', 'verified', 'expired', 'failed')",
            name="upload_status_valid",
        ),
    )

    # 资源类型，例如 audio_record、inspection_image、report_attachment。
    resource_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    # 本次上传所属的业务资源ID。
    resource_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    # 上传文件所属的组织机构，用于数据权限隔离。
    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organization_units.id", ondelete="RESTRICT"),
        index=True,
    )

    # 文件在对象存储中的唯一对象键。
    object_key: Mapped[str] = mapped_column(
        String(1024),
        unique=True,
        nullable=False,
    )

    # 用户上传时的原始文件名。
    filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # 文件MIME类型，例如 audio/wav、image/jpeg。
    mime_type: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    # 文件大小，单位为字节。
    size_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # 客户端上传前计算的SHA-256摘要，用于完整性校验。
    client_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    # 服务端接收文件后重新计算的SHA-256摘要。
    server_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    # 上传状态，例如 pending、uploaded、verified、expired、failed。
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        index=True,
    )

    # 上传会话或签名上传地址的过期时间。
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# 按用户、操作和幂等键缓存首次结果，避免重试重复执行。
class IdempotencyRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "idempotency_records"

    __table_args__ = (
        # 同一个用户执行同一种操作时，相同幂等键只能存在一条记录。
        UniqueConstraint(
            "actor_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_key",
        ),
        CheckConstraint(
            "response_status IS NULL OR (response_status >= 100 AND response_status <= 599)",
            name="idempotency_response_status_valid",
        ),
    )

    # 发起操作的用户ID。
    actor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )

    # 业务操作名称，例如 audio.create、job.retry。
    operation: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    # 客户端提供的幂等键。
    idempotency_key: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    # 请求内容摘要，用于判断同一幂等键是否对应相同请求。
    request_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    # 第一次请求执行后的HTTP响应状态码。
    response_status: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # 第一次请求的响应内容，重复请求时可以直接返回。
    response_body: Mapped[dict | None] = mapped_column(
        JSON_DOCUMENT,
        nullable=True,
    )


# 保存提示词及输出Schema的不可覆盖历史版本。
class PromptTemplateVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "prompt_template_versions"

    __table_args__ = (
        # 同一个模板编号下，版本号不能重复。
        UniqueConstraint(
            "template_code",
            "version",
            name="uq_prompt_template_version",
        ),
        Index(
            "uq_prompt_template_active",
            "template_code",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
        CheckConstraint("version >= 1", name="prompt_version_positive"),
    )

    # 模板业务编号，例如 handover.summary、event.extract。
    template_code: Mapped[str] = mapped_column(
        String(100),
        index=True,
    )

    # 模板版本号，例如1、2、3。
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # 发送给模型的系统提示词。
    system_prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # 用户消息模板，可包含业务变量占位符。
    user_template: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # 模型输出必须满足的结构定义。
    output_schema: Mapped[dict] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )

    # 当前版本是否处于激活状态。
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    # 创建该模板版本的用户ID。
    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )


# 用稳定业务别名映射实际Provider和模型名称。
class ModelAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_aliases"
    capability: Mapped[str] = mapped_column(
        String(32), default="text", server_default="text", nullable=False
    )

    # 业务使用的模型别名，必须唯一。
    code: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
    )

    # 模型供应商，例如 qwen、openai、local_http。
    provider: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    # 供应商侧的实际模型名称。
    model_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    # 可选的固定模型快照或模型版本。
    model_snapshot: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    # 该模型别名当前是否允许使用。
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # 模型温度、最大Token数、超时等附加配置。
    config: Mapped[dict] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )


# 版本化保存热词、风险规则和报告模板等业务配置。
class BusinessConfigVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "business_config_versions"

    __table_args__ = (
        # 同一个配置编号下，版本号不能重复。
        UniqueConstraint(
            "config_code",
            "version",
            name="uq_business_config_version",
        ),
        Index(
            "uq_business_config_active",
            "config_code",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
        CheckConstraint("version >= 1", name="business_config_version_positive"),
    )

    # 配置业务编号，例如 asr.hotwords、event.classification。
    config_code: Mapped[str] = mapped_column(
        String(100),
        index=True,
    )

    # 配置类型，用于区分不同配置结构。
    config_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    # 配置版本号。
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # 当前版本的完整配置内容。
    payload: Mapped[dict] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )

    # 当前配置版本是否激活。
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    # 创建该配置版本的用户ID。
    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )


# 统一记录各业务模块的异步处理任务及其执行快照。
class AsyncJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "async_jobs"

    __table_args__ = (
        # 用于按照状态扫描最近更新的任务。
        Index(
            "ix_async_jobs_status_updated",
            "status",
            "updated_at",
        ),
        Index(
            "uq_async_jobs_active_resource",
            "job_type",
            "resource_type",
            "resource_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'waiting_input')"),
            sqlite_where=text("status IN ('queued', 'running', 'waiting_input')"),
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled')",
            name="async_job_status_valid",
        ),
        CheckConstraint("progress >= 0 AND progress <= 100", name="async_job_progress_valid"),
        CheckConstraint("attempt >= 1", name="async_job_attempt_positive"),
        CheckConstraint("lock_version >= 1", name="async_job_lock_version_positive"),
        # 用于查询某个业务资源关联的所有异步任务。
        Index(
            "ix_async_jobs_resource",
            "resource_type",
            "resource_id",
        ),
    )

    # 任务类型，例如 transcribe_audio、summarize_audio。
    job_type: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )

    # 任务处理的资源类型。
    resource_type: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )

    # 任务处理的业务资源ID。
    resource_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    # 任务所属组织机构。
    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organization_units.id", ondelete="RESTRICT"),
        index=True,
    )

    # 任务状态，例如 queued、running、succeeded、failed、cancelled。
    status: Mapped[str] = mapped_column(
        String(32),
        default="queued",
        nullable=False,
    )

    # 任务进度，通常为0到100。
    progress: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # 用于前端展示的任务进度说明。
    message: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    # 稳定的任务错误代码。
    error_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    # 任务失败时的详细错误信息。
    error_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # 用户是否请求取消任务。
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    # 当前任务尝试次数。
    attempt: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
    )

    # 重试任务所关联的上一次任务ID。
    parent_job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "async_jobs.id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    # 创建任务时保存的模型、提示词和业务配置快照。
    config_snapshot: Mapped[dict] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )

    # 发起任务的用户ID。
    requested_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Worker真正开始、完成和最近一次报告存活的时间，用于超时与失联检测。
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 执行当前任务的Worker标识以及用于并发更新检测的版本号。
    worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    execution_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


# 追加记录任务状态和进度，供时间线、SSE和故障排查使用。
class JobEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_events"

    __table_args__ = (
        # 按任务ID和创建时间查询任务事件时间线。
        Index(
            "ix_job_events_job_created",
            "job_id",
            "created_at",
        ),
    )

    # 事件所属的异步任务ID。
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "async_jobs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    # 事件类型，例如 queued、started、progress、failed、succeeded。
    event_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    # 事件的附加信息。
    payload: Mapped[dict] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )


# 记录每次模型调用的耗时、用量和结果。
class AICallLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ai_call_logs"
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True, nullable=True)
    capability: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        # 按任务ID和调用时间查询模型调用记录。
        Index(
            "ix_ai_call_logs_job",
            "job_id",
            "created_at",
        ),
        CheckConstraint("duration_ms >= 0", name="ai_call_duration_nonnegative"),
        CheckConstraint(
            "input_units IS NULL OR input_units >= 0", name="ai_call_input_nonnegative"
        ),
        CheckConstraint(
            "output_units IS NULL OR output_units >= 0", name="ai_call_output_nonnegative"
        ),
        CheckConstraint(
            "status IN ('succeeded', 'failed', 'timeout', 'cancelled')",
            name="ai_call_status_valid",
        ),
    )

    # 本次模型调用所属的异步任务。
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "async_jobs.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    # 模型供应商，例如 qwen、openai、local_http。
    provider: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    # 业务使用的模型别名。
    model_alias: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    # 实际调用的模型名称。
    model_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    # 供应商返回的请求ID，用于向供应商追踪问题。
    provider_request_id: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    # 模型调用耗时，单位为毫秒。
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # 输入Token数、音频时长或图片数量等输入计量值。
    input_units: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # 输出Token数或其他输出计量值。
    output_units: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # 调用状态，例如 succeeded、failed、timeout。
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    # 模型调用失败时的稳定错误代码。
    error_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )


# 保存可暂停、恢复和人工审批的工作流状态快照。
class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"

    __table_args__ = (
        # 根据工作流状态和更新时间扫描待处理工作流。
        Index(
            "ix_workflow_runs_status_updated",
            "status",
            "updated_at",
        ),
        CheckConstraint("lock_version >= 1", name="workflow_lock_version_positive"),
        CheckConstraint("schema_version >= 1", name="workflow_schema_version_positive"),
    )

    # 工作流类型，例如 event_review、report_review。
    workflow_type: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )

    # 工作流关联的业务资源类型。
    resource_type: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )

    # 工作流关联的业务资源ID。
    resource_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    # 工作流所属组织机构。
    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organization_units.id", ondelete="RESTRICT"),
        index=True,
    )

    # 工作流状态，例如 running、waiting_review、completed、failed。
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    # 工作流当前执行到的节点。
    current_node: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    # 工作流当前完整状态快照。
    state_snapshot: Mapped[dict] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )

    # 工作流引擎使用的唯一线程ID，用于恢复执行状态。
    thread_id: Mapped[str] = mapped_column(
        String(200),
        unique=True,
        nullable=False,
    )

    # 状态文档版本支持后续迁移，锁版本用于检测并发审批和恢复。
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


# 追加记录关键操作及前后数据；业务时间由occurred_at明确保存。
class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"

    __table_args__ = (
        # 查询某个业务资源的审计时间线。
        Index(
            "ix_audit_logs_resource",
            "resource_type",
            "resource_id",
            "occurred_at",
        ),
    )

    # 审计事件实际发生时间。
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # 执行操作的用户ID。
    actor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user_accounts.id", ondelete="RESTRICT"),
        index=True,
    )

    # 操作发生时用户所在或操作目标所属组织。
    organization_unit_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organization_units.id", ondelete="RESTRICT"),
        index=True,
    )

    # 操作名称，例如 report.publish、work_order.close。
    action: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    # 被操作资源的类型。
    resource_type: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )

    # 被操作资源的ID。
    resource_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    # 操作前的数据快照。
    before_data: Mapped[dict | None] = mapped_column(
        JSON_DOCUMENT,
        nullable=True,
    )

    # 操作后的数据快照。
    after_data: Mapped[dict | None] = mapped_column(
        JSON_DOCUMENT,
        nullable=True,
    )

    # 对应HTTP请求的请求追踪ID。
    request_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    # 用户执行敏感操作时填写的业务理由。
    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # 哈希链使审计记录被修改或删除后可以被离线校验发现。
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


# 与业务数据同事务写入待投递任务，提交后再可靠发送到RabbitMQ。
class TaskOutbox(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "task_outbox"

    __table_args__ = (
        # 同一种业务操作只能进入一次Outbox；跨job也能阻止重复投递。
        UniqueConstraint(
            "operation_key",
            name="uq_task_outbox_operation",
        ),
        # 发布器按状态和可用时间扫描待投递消息。
        Index(
            "ix_task_outbox_publish",
            "status",
            "available_at",
        ),
        CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed')",
            name="task_outbox_status_valid",
        ),
        CheckConstraint("publish_attempts >= 0", name="task_outbox_attempts_nonnegative"),
    )

    # Outbox消息对应的异步任务ID。
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "async_jobs.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    # 任务内部的幂等操作键。
    operation_key: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    # 需要投递的Celery任务名称。
    task_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    # Celery/RabbitMQ目标队列名称。
    queue: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )

    # 发送给异步任务的参数。
    payload: Mapped[dict] = mapped_column(
        JSON_DOCUMENT,
        default=dict,
        nullable=False,
    )

    # Outbox投递状态，例如 pending、publishing、published、failed。
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        nullable=False,
    )

    # 当前已经尝试投递的次数。
    publish_attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # 该消息最早允许被发布的时间，用于延迟和重试。
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # 消息成功投递到消息队列的时间。
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # 最近一次消息投递失败的错误信息。
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

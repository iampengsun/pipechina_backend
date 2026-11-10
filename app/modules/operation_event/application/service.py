from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.knowledge import KnowledgeResourceManager
from app.modules.handover.domain.models import (
    AudioRecord,
    AudioTranscriptVersion,
    ManualOperationRecord,
    ManualOperationRecordVersion,
)
from app.modules.maintenance_order.domain.models import AbnormalityAssessment
from app.modules.operation_event.application.investigation import run_multi_agent_assessment
from app.modules.operation_event.application.investigation_tools import (
    authorize_knowledge_evidence,
    build_investigation_tools,
)
from app.modules.operation_event.domain.agent_models import AgentRun
from app.modules.operation_event.domain.models import (
    EventSourceLink,
    ProductionEvent,
    ProductionEventVersion,
)
from app.modules.operation_event.domain.schemas import (
    EventExtractionCreate,
    EventUpdate,
    MergeEventsInput,
    SplitEventInput,
)
from app.modules.operation_event.infrastructure.repository import OperationEventRepository
from app.ports.models import EventCandidate, EventCandidateList
from app.ports.text import TextLLMProvider
from app.shared.errors import ConflictError, NotFoundError
from app.shared.platform.execution import execution_lease, guard_execution
from app.shared.platform.leased_jobs import leased_job
from app.shared.platform.models import AsyncJob, WorkflowRun
from app.shared.platform.runtime import (
    build_runtime_snapshot,
    render_user_prompt,
    system_prompt,
    text_provider_for_job,
)
from app.shared.platform.service import create_job, update_job
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser


async def get_event(session: AsyncSession, event_id: UUID) -> ProductionEvent:
    """按照生产事件id进行生产事件的读取，不存在时转换为业务未找到错误。"""
    event = await OperationEventRepository(session).get_event(event_id)
    if event is None:
        raise NotFoundError("production_event", event_id)
    return event  # 返回事件实体


async def _source_text(
    session: AsyncSession, payload: EventExtractionCreate, user: CurrentUser
) -> tuple[str, UUID, UUID, str]:
    """解析抽取来源并返回固定版本正文；临时文字先保存为人工记录。"""
    if payload.source_type == "raw_text":  # 临时文本先固化，后台任务不依赖请求内存
        if not payload.raw_text:  # 临时来源必须有非空正文
            raise ConflictError("raw_text is required for raw_text source")
        record = ManualOperationRecord(
            organization_unit_id=user.organization_unit_id,  # 所属组织单元，用于数据范围隔离
            occurred_at=datetime.now(UTC).isoformat(),  # 确认后的发生时间，可为空
            record_type="event_extraction_input",
            business_status="confirmed",  # 事件业务状态
            created_by=user.user_id,  # 创建用户标识
        )
        session.add(record)  # 将临时输入的人工记录加入当前事务
        await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
        # 另外，只有flush插入了上面的主记录，才会产生主记录的的uuid，下面的版本才能引用
        raw_version = ManualOperationRecordVersion(
            record_id=record.id,
            version=1,  # 所属记录内的版本号
            content=payload.raw_text,  # 取证时保存的内容快照
            structured_data={"source_type": "raw_text"},  # 扩展结构化内容
            created_by=user.user_id,  # 创建用户标识
        )
        session.add(raw_version)  # 将临时文字版本加入当前事务
        await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
        record.current_version_id = raw_version.id  # 将临时文字的人工记录指向固定正文版本
        return payload.raw_text, record.id, raw_version.id, "manual_operation"
    if payload.source_type == "audio_transcript":  # 转写来源由版本反查录音并检查权限
        transcript_version = (  # 这里其实是到AudioTranscriptVersion表中按照id查询转写后的那一行
            await session.get(AudioTranscriptVersion, payload.source_version_id)
            if payload.source_version_id
            else None  # 这里是一个三元运算符，即 值1 if 条件 else 值2
        )
        if transcript_version is None:
            raise NotFoundError("audio_transcript_version", payload.source_version_id)
        audio = await session.get(AudioRecord, transcript_version.audio_record_id)
        if audio is None:  # 按照刚才的转写记录的id去找一下对应的音频，如果音频不存在也报错
            raise NotFoundError("audio_record", transcript_version.audio_record_id)
        require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
            user,
            audio.organization_unit_id,
            Permissions.EVENT_EXTRACT,
            owner_id=audio.created_by,
        )
        return (
            transcript_version.full_text,  # 转写后的文字
            transcript_version.audio_record_id,  # 转写对应的音频的id
            transcript_version.id,  # 转写文字是第几版
            payload.source_type,  # 这段文字的来源是什么
        )
    # 经过前面两段临时文字和音频转写，三大来源只剩下最后一个，就是真正的人工手工记录
    manual_record = await session.get(ManualOperationRecord, payload.source_id)
    if manual_record is None:
        raise NotFoundError("manual_operation_record", payload.source_id)
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user,
        manual_record.organization_unit_id,
        Permissions.EVENT_EXTRACT,
        owner_id=manual_record.created_by,
    )
    version_id = (
        payload.source_version_id or manual_record.current_version_id
    )  # 未指定版本时采用人工记录的当前版本
    manual_version = await session.get(ManualOperationRecordVersion, version_id)
    if manual_version is None:
        raise NotFoundError("manual_operation_record_version", version_id)
    if manual_version.record_id != manual_record.id:
        raise ConflictError("manual operation version does not belong to source record")
    return manual_version.content, manual_record.id, manual_version.id, payload.source_type


async def load_persisted_source(
    session: AsyncSession, *, source_type: str, source_version_id: UUID
) -> str:
    """按任务快照中的来源版本加载正文，避免使用排队后更新的内容。"""
    if source_type == "audio_transcript":  # 根据任务来源类型选择固定正文的存储表
        transcript_version = await session.get(AudioTranscriptVersion, source_version_id)
        if transcript_version is None:
            raise NotFoundError("audio_transcript_version", source_version_id)
        return transcript_version.full_text

    manual_version = await session.get(ManualOperationRecordVersion, source_version_id)
    if manual_version is None:
        raise NotFoundError("manual_operation_record_version", source_version_id)
    return manual_version.content


async def _persist_candidate(  # 连续插入3个本模块中的实体
    session: AsyncSession,
    *,
    candidate: EventCandidate,
    user: CurrentUser,
    source_type: str,
    source_id: UUID | None,
    source_version_id: UUID | None,
) -> ProductionEvent:
    """保存候选事件、首个 AI 版本及可选来源链接，由调用者提交事务。"""
    event = ProductionEvent(
        organization_unit_id=user.organization_unit_id,  # 所属组织单元，用于数据范围隔离
        title=candidate.title,  # 事件或条目的标题
        event_type=candidate.event_type,  # 生产事件类型
        severity=candidate.severity,  # 事件严重度
        occurred_at=None,  # 确认后的发生时间，可为空
        business_status="candidate",  # 事件业务状态，candidate表示这是一个候选状态
        created_by=user.user_id,  # 创建用户标识
    )
    session.add(event)  # 将候选事件加入当前事务
    await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
    version = ProductionEventVersion(
        event_id=event.id,  # 关联生产事件标识
        version=1,  # 所属记录内的版本号
        source="ai",  # 版本生成方式
        description=candidate.description,  # 该版本或条目的完整描述
        structured_data={"occurred_at_text": candidate.occurred_at_text},  # 扩展结构化内容
        confidence=candidate.confidence,  # 抽取置信度
        created_by=user.user_id,  # 创建用户标识
    )
    session.add(version)  # 将新事件版本加入当前事务
    await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
    event.current_version_id = version.id
    # 这里虽然是变量之间的赋值，但是这些对象都代表数据库表格，所以实际上做了表格的修改
    if (
        source_id
    ):  # source_id是原始内容来源在数据库中的主键，比如某个音频，手工书写的交接班记录等等的id
        session.add(  # 将来源追溯链接加入当前事务
            EventSourceLink(
                event_id=event.id,  # 关联生产事件标识
                source_type=source_type,  # 来源对象类型
                source_id=source_id,  # 来源主记录标识
                source_version_id=source_version_id,  # 固定来源版本标识
                evidence_text=candidate.evidence_text,  # 支持该候选的证据文字
                evidence_locator={},  # 证据位置扩展信息
            )
        )
    return event  # 返回事件实体


async def extract_events(
    session: AsyncSession,
    *,
    payload: EventExtractionCreate,
    user: CurrentUser,
    provider: TextLLMProvider,
    inline: bool,
) -> tuple[UUID, list[ProductionEvent]]:  # job.id, events这就是一个消息的样子，当然它会变成JSON
    """创建抽取任务，送入消息队列，即给消息队列发送一个消息（JSON）。
    消息队列收到消息后，本模块中的extract_events_task会自动发现消息并开始执行"""
    text, source_id, version_id, source_type = await _source_text(session, payload, user)
    job = await create_job(  # 创建任务及执行配置，异步模式同时准备 Outbox 投递
        session,
        user=user,
        job_type="event_extraction",
        resource_type=source_type,
        resource_id=source_id,
        task_name="app.modules.operation_event.extract_events",
        queue="ai_text",
        config_snapshot=await build_runtime_snapshot(  # 本次执行采用的配置快照
            session,
            job_type="event_extraction",
            provider=provider,
            base={"source_type": source_type, "source_version_id": str(version_id)},
        ),
        enqueue=not inline,
    )
    if not inline:  # 异步模式提交任务后由 Worker 消费
        await session.commit()  # 提交当前业务状态与关联记录
        return job.id, []
    events = await execute_event_extraction(
        session,
        job=job,
        text=text,
        source_type=source_type,  # 来源对象类型
        source_id=source_id,  # 来源主记录标识
        source_version_id=version_id,  # 固定来源版本标识
        user=user,
        provider=provider,
    )
    return job.id, events


@leased_job
async def execute_event_extraction(
    session: AsyncSession,
    *,
    job: AsyncJob,
    text: str,
    source_type: str,
    source_id: UUID,
    source_version_id: UUID,
    user: CurrentUser,
    provider: TextLLMProvider,
) -> list[ProductionEvent]:
    """调用文字模型抽取候选事件，保存结果并更新任务终态。"""
    if job.status in {"succeeded", "failed", "cancelled"}:  # 终态任务不再执行业务，不能替代并发互斥
        return []  # 返回空列表，不再创建候选
    if job.cancel_requested:  # 处理当前已可见的任务取消请求
        await update_job(
            session, job, status="cancelled", progress=job.progress
        )  # 同步任务状态、进度及关联通知记录
        await session.commit()  # 提交当前业务状态与关联记录
        return []  # 返回空列表，不再创建候选
    await update_job(
        session, job, status="running", progress=20, message="extracting events"
    )  # 更新一个任务的状态(running)、进度(20)及这个进度下到底做了什么事情(extracting events)
    await session.commit()
    provider = text_provider_for_job(session, job, provider)  # 按任务配置快照选择文字模型
    result = await provider.generate_structured(  # 调用模型并按指定输出模型校验返回结构
        operation="event_extraction",
        system_prompt=system_prompt(job.config_snapshot),
        user_prompt=render_user_prompt(job.config_snapshot, text),
        response_model=EventCandidateList,
    )
    # 结构化模型输出中的 events 是已校验的候选事件列表。
    candidates = result.events
    events = [
        await _persist_candidate(  # 保存候选及固定来源版本关系
            session,
            candidate=candidate,
            user=user,
            source_type=source_type,  # 来源对象类型
            source_id=source_id,  # 来源主记录标识
            source_version_id=source_version_id,  # 固定来源版本标识
        )  # 这里是一个推导式，大模型可能从刚才的文字中抽取了多个事件
        for candidate in candidates
    ]
    await update_job(
        session,
        job,
        status="succeeded",
        progress=100,
        message="events persisted",
        result={"event_ids": [str(event.id) for event in events]},
    )  # 同步任务状态、进度及关联通知记录
    await session.commit()  # 提交当前业务状态与关联记录
    return events  # 返回本次候选事件列表


async def update_event(
    session: AsyncSession, event: ProductionEvent, payload: EventUpdate, user: CurrentUser
) -> ProductionEventVersion:
    """校验编辑权限与状态，新增人工版本并更新事件主记录。"""
    await session.refresh(event, with_for_update=True)
    if (
        payload.expected_version_id is not None
        and payload.expected_version_id != event.current_version_id
    ):
        raise ConflictError("event version changed")
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user,
        event.organization_unit_id,
        Permissions.EVENT_EDIT,
        owner_id=event.created_by,
    )
    if event.business_status in {"rejected", "superseded"}:  # 禁止修改已驳回或已被取代的事件
        raise ConflictError("rejected or superseded event cannot be edited")
    version_no = (
        await session.scalar(
            select(func.max(ProductionEventVersion.version)).where(
                ProductionEventVersion.event_id == event.id
            )
        )
        or 0
    ) + 1
    version = ProductionEventVersion(
        event_id=event.id,  # 关联生产事件标识
        version=version_no,  # 所属记录内的版本号
        source="manual",  # 版本生成方式
        description=payload.description,  # 该版本或条目的完整描述
        structured_data=payload.structured_data,  # 扩展结构化内容
        created_by=user.user_id,  # 创建用户标识
    )
    session.add(version)  # 将新事件版本加入当前事务
    await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务

    # 下面几行都是为了修改ProductionEvent表格的内容（因为现在多了版本）
    event.title = payload.title
    event.event_type = payload.event_type
    event.severity = payload.severity
    event.occurred_at = payload.occurred_at
    event.business_status = "candidate"
    event.confirmed_by = None
    event.current_version_id = version.id  # 将事件指向本次新建的版本
    await session.commit()  # 提交当前业务状态与关联记录
    return version  # 返回新增的事件版本


async def classify_event(
    session: AsyncSession,
    *,
    event: ProductionEvent,
    user: CurrentUser,
    provider: TextLLMProvider,
    inline: bool,
    knowledge: KnowledgeResourceManager | None = None,
    site_context=None,
    options=None,
    invocation_id: UUID | None = None,
    invocation_snapshot: dict | None = None,
) -> tuple[UUID, WorkflowRun | None, AbnormalityAssessment | None]:
    """为已确认事件创建调查任务和运行记录，复用已有活动分类任务。"""
    await session.refresh(event, with_for_update=True)  # 锁定并重读事件，串行检查当前状态和版本
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user,
        event.organization_unit_id,
        Permissions.EVENT_CLASSIFY,
        owner_id=event.created_by,
    )
    if event.business_status != "confirmed":  # 只有已确认事件可以进入调查
        raise ConflictError("event must be confirmed before classification")
    active = await session.scalar(
        select(AsyncJob).where(
            AsyncJob.job_type == "event_classification",
            AsyncJob.resource_id == event.id,
            AsyncJob.status.in_(("queued", "running", "waiting_input")),
        )
    )
    if active is not None:  # 复用尚未结束的分类任务，避免新增孤立运行
        await session.commit()  # 提交当前业务状态与关联记录
        return active.id, None, None
    version = await session.get(ProductionEventVersion, event.current_version_id)
    if version is None:
        raise ConflictError("event has no current version")
    from app.modules.operation_event.application.memory import case_context
    from app.modules.operation_event.domain.memory_models import ExperienceMemory

    context = await case_context(session, user, event, for_model=True)
    has_experience = await session.scalar(
        select(ExperienceMemory.id)
        .where(
            ExperienceMemory.organization_unit_id == event.organization_unit_id,
            ExperienceMemory.status == "approved",
            ExperienceMemory.expires_at > datetime.now(UTC),
        )
        .limit(1)
    )
    from app.modules.operation_event.application.skills import catalog
    from app.modules.operation_event.domain.investigation_schemas import InvestigationOptions

    selected_options = options or InvestigationOptions()
    runtime = await build_runtime_snapshot(
        session, job_type="event_classification", provider=provider
    )
    pricing = runtime.get("model_config", {}).get("pricing_microusd_per_million", {})
    pricing = {key: int(pricing.get(key, 0)) for key in ("input", "output")}
    if any(value < 0 for value in pricing.values()):
        raise ConflictError("model prices cannot be negative")
    if selected_options.max_cost_microusd is not None and not all(pricing.values()):
        raise ConflictError("cost caps require positive input/output prices in model alias config")
    if selected_options.prompt_candidate_id:
        from app.shared.errors import PermissionDeniedError
        from app.shared.platform.models import PromptTemplateVersion

        if not user.has_permission(Permissions.ADMIN_PROMPT):
            raise PermissionDeniedError(Permissions.ADMIN_PROMPT)
        candidate = await session.get(PromptTemplateVersion, selected_options.prompt_candidate_id)
        if candidate is None or candidate.template_code != "event.classify":
            raise ConflictError("unknown investigation prompt candidate")
        runtime["prompt"] = {
            "id": str(candidate.id),
            "code": candidate.template_code,
            "version": candidate.version,
            "system_prompt": candidate.system_prompt,
            "user_template": candidate.user_template,
            "output_schema": candidate.output_schema,
        }
    run = AgentRun(
        **({"id": invocation_id} if invocation_id else {}),
        event_id=event.id,  # 关联生产事件标识
        event_version_id=version.id,  # 本次调查固定的事件版本
        organization_unit_id=event.organization_unit_id,  # 所属组织单元，用于数据范围隔离
        status="queued",  # 当前执行状态
        current_stage="coordinator",  # 运行阶段标记
        config_snapshot={  # 本次执行采用的配置快照
            **({"mcp_task": invocation_snapshot} if invocation_snapshot else {}),
            "limits": selected_options.model_dump(mode="json"),
            "requested_skills": selected_options.skills,
            "skill_catalog": {name: skill.digest for name, skill in catalog().items()},
            "roles": ["coordinator", "evidence", "risk", "plan", "critic"],
            "case_context": context,
            "observation_revision": context["observation_revision"],
            "evidence_step_budget": 10
            if context["summaries"] or context["observations"] or has_experience
            else 6,
            "pricing_microusd_per_million": pricing,
        },
        created_by=user.user_id,  # 创建用户标识
        trace_id=uuid4(),  # 整次调查的链路标识
    )
    session.add(run)  # 将调查运行加入当前事务
    await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
    job = await create_job(  # 创建任务及执行配置，异步模式同时准备 Outbox 投递
        session,
        user=user,
        job_type="event_classification",
        resource_type="production_event",
        resource_id=event.id,
        task_name="app.modules.operation_event.classify_event",
        queue="ai_text",
        config_snapshot={
            **runtime,
            "event_version_id": str(version.id),
            "agent_run_id": str(run.id),
            "retry_handler": "event_classification",
        },
        enqueue=not inline,
    )
    run.config_snapshot = {**run.config_snapshot, "runtime": job.config_snapshot}
    run.job_id = job.id  # 关联调查运行与刚创建的分类任务
    if not inline:  # 异步模式提交任务后由 Worker 消费
        await session.commit()  # 提交当前业务状态与关联记录
        return job.id, None, None
    await session.commit()  # 提交当前业务状态与关联记录
    workflow, assessment = await execute_event_classification(
        session,
        job=job,
        event=event,
        version=version,  # 本次任务固定的事件版本实体
        user=user,
        provider=provider,
        knowledge=knowledge,
        site_context=site_context,
    )
    return job.id, workflow, assessment


async def execute_event_classification(
    session: AsyncSession,
    *,
    job: AsyncJob,
    event: ProductionEvent,
    version: ProductionEventVersion,
    user: CurrentUser,
    provider: TextLLMProvider,
    knowledge: KnowledgeResourceManager | None = None,
    site_context=None,
) -> tuple[WorkflowRun | None, AbnormalityAssessment | None]:
    """持有独占租约执行调查，并用执行令牌保护异常后的状态写入。"""
    factory = async_sessionmaker(session.bind, expire_on_commit=False, autoflush=False)
    job_id = job.id
    token = None
    try:
        async with execution_lease(
            factory, job_id
        ) as token:  # 领取独占执行权，后台续租并在退出时收尾
            try:
                await session.refresh(job)  # 重新读取任务状态，感知取消等外部变更
                return await _execute_event_classification(
                    session,
                    job=job,
                    event=event,
                    version=version,  # 本次任务固定的事件版本实体
                    user=user,
                    provider=provider,
                    execution_token=token,
                    knowledge=knowledge,
                    site_context=site_context,
                )
            except BaseException:
                await session.rollback()
                raise
    except BaseException:
        await session.rollback()  # 回滚当前事务，避免保留不完整业务结果
        if token is not None:
            async with factory() as failure_session:
                failed = await failure_session.get(AsyncJob, job_id, with_for_update=True)
                if (
                    failed and failed.execution_token == token and failed.status == "running"
                ):  # 只允许本次执行者将仍在运行的任务标记失败
                    status = "cancelled" if failed.cancel_requested else "failed"
                    await update_job(  # 同步任务状态、进度及关联通知记录
                        failure_session,
                        failed,
                        status=status,  # 当前执行状态
                        progress=failed.progress,
                        error_code="INVESTIGATION_FAILED" if status == "failed" else None,
                    )
                    run = await failure_session.get(
                        AgentRun, UUID(failed.config_snapshot["agent_run_id"])
                    )
                    if run:
                        run.status = status
                        run.completed_at = datetime.now(UTC)
                    await failure_session.commit()  # 独立提交持有执行权的失败或取消状态
        raise


async def _execute_event_classification(
    session: AsyncSession,
    *,
    job: AsyncJob,
    event: ProductionEvent,
    version: ProductionEventVersion,
    user: CurrentUser,
    provider: TextLLMProvider,
    execution_token: UUID,
    knowledge: KnowledgeResourceManager | None = None,
    site_context=None,
) -> tuple[WorkflowRun | None, AbnormalityAssessment | None]:
    """调查固定事件版本，复核执行权和权限后保存待审核评估与工作流。"""
    if job.status in {"succeeded", "failed", "cancelled"}:  # 终态任务不再执行业务，不能替代并发互斥
        return None, None  # 返回空工作流与评估，表示未生成业务产物
    if job.cancel_requested:  # 处理当前已可见的任务取消请求
        await update_job(
            session, job, status="cancelled", progress=job.progress
        )  # 同步任务状态、进度及关联通知记录
        await session.commit()  # 提交当前业务状态与关联记录
        return None, None  # 返回空工作流与评估，表示未生成业务产物
    await update_job(  # 同步任务状态、进度及关联通知记录
        session,
        job,
        status="running",
        progress=max(10, job.progress),
        message="running multi-agent investigation",
    )
    provider = text_provider_for_job(session, job, provider)  # 按任务配置快照选择文字模型
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user, event.organization_unit_id, Permissions.EVENT_CLASSIFY, owner_id=event.created_by
    )
    if (
        event.business_status != "confirmed"
        or version.event_id != event.id
        or event.current_version_id != version.id
    ):
        raise ConflictError("classification requires the current confirmed event version")
    await session.commit()  # 提交当前业务状态与关联记录
    run_id = UUID(str(job.config_snapshot["agent_run_id"]))
    run = await session.get(AgentRun, run_id)
    if run is None or run.event_id != event.id or run.event_version_id != version.id:
        raise ConflictError("agent run does not match the classification snapshot")
    from app.shared.worker_runtime import job_user

    current_user = await job_user(session, job)  # 重新加载任务创建人的当前授权
    tools = build_investigation_tools(
        session,
        current_user,
        event,
        knowledge=knowledge,
        site_context=site_context,
        text=provider,
        refresh_user=lambda: job_user(session, job),
        run_id=run.id,
    )  # 绑定当前授权上下文和事件组织的只读工具
    try:
        proposal = await run_multi_agent_assessment(  # 执行调查图并生成待审核建议
            session,
            run=run,
            event=event,
            version=version,  # 本次任务固定的事件版本实体
            job=job,
            provider=provider,
            tools=tools,
            execution_token=execution_token,
        )
    except Exception as exc:
        await session.refresh(run)  # 重新读取调查运行，检查外部取消请求
        if run.cancel_requested:  # 识别调查运行层面的取消请求
            run.status = "cancelled"
            await update_job(
                session, job, status="cancelled", progress=job.progress
            )  # 同步任务状态、进度及关联通知记录
            await session.commit()  # 提交当前业务状态与关联记录
            return None, None  # 返回空工作流与评估，表示未生成业务产物
        run.status = "failed"
        run.error_detail = str(exc)[:2000]
        run.completed_at = datetime.now(UTC)
        raise
    if proposal is None:
        return None, None
    await session.refresh(job)  # 重新读取任务状态，感知取消等外部变更
    if job.cancel_requested:  # 处理当前已可见的任务取消请求
        await update_job(
            session, job, status="cancelled", progress=job.progress
        )  # 同步任务状态、进度及关联通知记录
        await session.commit()  # 提交当前业务状态与关联记录
        return None, None  # 返回空工作流与评估，表示未生成业务产物
    job = await guard_execution(
        session, job.id, execution_token
    )  # 提交结果前校验租约令牌，阻止旧执行者写入
    await session.refresh(event, with_for_update=True)  # 锁定并重读事件，串行检查当前状态和版本
    current_user = await job_user(session, job)  # 重新加载任务创建人的当前授权
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        current_user,
        event.organization_unit_id,
        Permissions.EVENT_CLASSIFY,
        owner_id=event.created_by,
    )
    if event.business_status != "confirmed" or event.current_version_id != version.id:
        raise ConflictError("event changed during assessment")
    from app.modules.operation_event.application.memory import (
        observation_revision,
        save_case_memory,
    )

    if run.config_snapshot.get("observation_revision") != await observation_revision(
        session, event
    ):
        raise ConflictError("confirmed case observations changed during investigation")
    await authorize_knowledge_evidence(session, current_user, run.id, require_current_context=True)
    await save_case_memory(session, run, proposal)
    assessment = AbnormalityAssessment(
        event_id=event.id,  # 关联生产事件标识
        organization_unit_id=event.organization_unit_id,  # 所属组织单元，用于数据范围隔离
        category=proposal.category,  # 异常分类
        risk_level=proposal.risk_level,  # 风险等级
        rationale=proposal.rationale,  # 分类分级依据
        recommended_action=proposal.recommended_action,  # 建议的处置措施
        review_status="pending",  # 建议的人工审核状态
    )
    session.add(assessment)  # 将待审核评估加入当前事务
    await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
    workflow = WorkflowRun(
        workflow_type="maintenance_order",
        resource_type="production_event",
        resource_id=event.id,
        organization_unit_id=event.organization_unit_id,  # 所属组织单元，用于数据范围隔离
        status="awaiting_review",  # 当前执行状态
        current_node="human_review",
        state_snapshot={
            "event_id": str(event.id),
            "assessment_id": str(assessment.id),
            "category": assessment.category,
            "risk_level": assessment.risk_level,
            "node": "human_review",
            "event_version_id": str(version.id),
            "agent_run_id": str(run.id),
            "observation_revision": run.config_snapshot["observation_revision"],
            "proposal_id": str(proposal.id),
            "agent": {
                "kind": "multi_agent",
                "run_id": str(run.id),
                "evidence_ids": proposal.evidence_ids,
                "unknowns": proposal.unknowns,
                "conflicts": proposal.conflicts,
            },
        },
        thread_id=f"maintenance:{event.id}:{assessment.id}",
    )
    session.add(workflow)  # 将人工审核工作流加入当前事务
    await session.flush()
    await update_job(  # 同步任务状态、进度及关联通知记录
        session,
        job,
        status="succeeded",
        progress=100,
        message="awaiting human review",
        result={
            "workflow_id": str(workflow.id),
            "assessment_id": str(assessment.id),
            "agent_run_id": str(run.id),
        },
    )
    await session.commit()  # 提交当前业务状态与关联记录
    return workflow, assessment


async def merge_events(
    session: AsyncSession, payload: MergeEventsInput, user: CurrentUser
) -> ProductionEvent:
    """合并同组织的有效事件为新候选，并将原事件标记为已取代。"""
    events = [
        await get_event(session, item) for item in payload.event_ids
    ]  # 逐一加载待合并事件，缺失即中断
    for event in events:
        require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
            user,
            event.organization_unit_id,
            Permissions.EVENT_TRANSFORM,
            owner_id=event.created_by,
        )
        if event.business_status not in {"candidate", "confirmed"}:  # 合并或拆分只接受仍有效的事件
            raise ConflictError("only candidate or confirmed events can be merged")
    organization_ids = {event.organization_unit_id for event in events}  # 汇总所有原事件的所属组织
    if len(organization_ids) != 1:  # 拒绝跨组织合并事件
        raise ConflictError("events from different organizations cannot be merged")
    merged = ProductionEvent(
        organization_unit_id=events[0].organization_unit_id,  # 所属组织单元，用于数据范围隔离
        title=payload.title,  # 事件或条目的标题
        event_type=events[0].event_type,  # 生产事件类型
        severity=max(  # 事件严重度
            (event.severity for event in events), key=["low", "medium", "high", "critical"].index
        ),
        occurred_at=min(
            (event.occurred_at for event in events if event.occurred_at), default=None
        ),  # 确认后的发生时间，可为空
        business_status="candidate",  # 事件业务状态
        created_by=user.user_id,  # 创建用户标识
    )
    session.add(merged)  # 将合并事件加入当前事务
    await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
    version = ProductionEventVersion(
        event_id=merged.id,  # 关联生产事件标识
        version=1,  # 所属记录内的版本号
        source="merge",  # 版本生成方式
        description="\n".join(event.title for event in events),  # 该版本或条目的完整描述
        structured_data={"merged_event_ids": [str(event.id) for event in events]},  # 扩展结构化内容
        created_by=user.user_id,  # 创建用户标识
    )
    session.add(version)  # 将新事件版本加入当前事务
    await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
    merged.current_version_id = version.id  # 将合并事件指向其首个版本
    for event in events:
        event.business_status = "superseded"  # 原事件保留用于追溯，但不再作为有效事件
    await session.commit()  # 提交当前业务状态与关联记录
    return merged  # 返回合并后的候选事件


async def split_event(
    session: AsyncSession, event: ProductionEvent, payload: SplitEventInput, user: CurrentUser
) -> list[ProductionEvent]:
    """按输入条目创建候选并关联原事件版本，将原事件标记为已取代。"""
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user,
        event.organization_unit_id,
        Permissions.EVENT_TRANSFORM,
        owner_id=event.created_by,
    )
    if event.business_status not in {"candidate", "confirmed"}:  # 合并或拆分只接受仍有效的事件
        raise ConflictError("event cannot be split in current status")
    results = []
    for item in payload.items:
        candidate = EventCandidate(
            title=item.title,  # 事件或条目的标题
            event_type=item.event_type,  # 生产事件类型
            occurred_at_text=None,
            description=item.description,  # 该版本或条目的完整描述
            severity=item.severity,  # 事件严重度
            confidence=1.0,  # 抽取置信度
            evidence_text=f"split from {event.id}",  # 支持该候选的证据文字
        )
        results.append(
            await _persist_candidate(  # 保存候选及固定来源版本关系
                session,
                candidate=candidate,
                user=user,
                source_type="production_event",  # 来源对象类型
                source_id=event.id,  # 来源主记录标识
                source_version_id=event.current_version_id,  # 固定来源版本标识
            )
        )
    event.business_status = "superseded"  # 原事件保留用于追溯，但不再作为有效事件
    await session.commit()  # 提交当前业务状态与关联记录
    return results  # 返回拆分后的候选列表

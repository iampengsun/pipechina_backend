from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.modules.operation_event.application.service import (
    classify_event,
    extract_events,
    get_event,
    merge_events,
    split_event,
    update_event,
)
from app.modules.operation_event.domain.agent_models import AgentRun
from app.modules.operation_event.domain.investigation_schemas import (
    InvestigationOptions,
    ResumeInvestigation,
)
from app.modules.operation_event.domain.models import ProductionEvent, ProductionEventVersion
from app.modules.operation_event.domain.optimization_schemas import (
    OptimizationCreate,
    PublishOptimization,
)
from app.modules.operation_event.domain.schemas import (
    EventExtractionCreate,
    EventReviewInput,
    EventUpdate,
    EventView,
    MergeEventsInput,
    RejectInput,
    SplitEventInput,
)
from app.shared.db import SessionDep
from app.shared.errors import ConflictError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import (
    data_scope_clause,
    require_data_scope,
    require_permission,
)
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(tags=["operation-event"])


@router.post("/event-extractions", status_code=202)
async def post_event_extraction(
    payload: EventExtractionCreate,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_EXTRACT))],
) -> dict:
    """接收抽取请求，返回任务标识及内联执行生成的事件标识。"""
    job_id, events = await extract_events(
        session,
        payload=payload,
        user=user,
        provider=request.app.state.providers.text,
        inline=request.app.state.settings.run_tasks_inline,
    )
    return {"job_id": str(job_id), "event_ids": [str(event.id) for event in events]}


@router.get("/events", response_model=list[EventView])
async def list_events(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_READ))],
) -> list[EventView]:
    """查询用户可见的最近一百条事件并转换为响应视图。"""
    statement = select(ProductionEvent).order_by(ProductionEvent.created_at.desc()).limit(100)
    statement = statement.where(
        data_scope_clause(  # 在 SQL 中约束用户可见的数据范围
            user,
            Permissions.EVENT_READ,
            ProductionEvent.organization_unit_id,
            owner_column=ProductionEvent.created_by,
        )
    )
    rows = await session.scalars(statement)
    return [EventView.model_validate(row) for row in rows]


@router.get("/events/{event_id}", response_model=EventView)
async def read_event(
    event_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_READ))],
) -> EventView:
    """校验事件读取范围并返回事件主记录视图。"""
    event = await get_event(session, event_id)
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user,
        event.organization_unit_id,
        Permissions.EVENT_READ,
        owner_id=event.created_by,
    )
    return EventView.model_validate(event)


@router.get("/events/{event_id}/versions")
async def read_event_versions(
    event_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_READ))],
) -> list[dict]:
    """校验事件读取范围，按版本号返回历史描述与结构化数据。"""
    event = await get_event(session, event_id)
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user,
        event.organization_unit_id,
        Permissions.EVENT_READ,
        owner_id=event.created_by,
    )
    rows = await session.scalars(
        select(ProductionEventVersion)
        .where(ProductionEventVersion.event_id == event.id)
        .order_by(ProductionEventVersion.version)
    )
    return [
        {
            "id": str(row.id),
            "version": row.version,
            "source": row.source,
            "description": row.description,
            "structured_data": row.structured_data,
        }
        for row in rows
    ]


@router.put("/events/{event_id}")
async def put_event(
    event_id: UUID,
    payload: EventUpdate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_EDIT))],
) -> dict:
    """将人工修改交给业务服务，返回新增版本标识和版本号。"""
    version = await update_event(session, await get_event(session, event_id), payload, user)
    return {"version_id": str(version.id), "version": version.version}


@router.post("/events/{event_id}:confirm")
async def confirm_event(
    event_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_REVIEW))],
    payload: EventReviewInput | None = None,
) -> dict:
    """确认候选事件，记录确认人及审计日志后提交状态。"""
    event = await get_event(session, event_id)
    await session.refresh(event, with_for_update=True)
    if payload and payload.expected_version_id != event.current_version_id:
        raise ConflictError("event version changed")
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user,
        event.organization_unit_id,
        Permissions.EVENT_REVIEW,
        owner_id=event.created_by,
    )
    if event.business_status != "candidate":  # 确认和驳回仅适用于候选状态
        raise ConflictError("only candidate event can be confirmed")
    event.business_status = "confirmed"  # 将人工审核通过的候选转为已确认事件
    event.confirmed_by = user.user_id  # 记录执行确认操作的用户
    await add_audit(  # 记录人工审核动作及操作者，随业务事务提交
        session,
        user=user,
        action="production_event.confirm",  # 步骤动作或取证决策
        resource_type="production_event",
        resource_id=event.id,
    )
    await session.commit()  # 提交当前业务状态与关联记录
    return {"id": str(event.id), "business_status": event.business_status}


@router.post("/events/{event_id}:reject")
async def reject_event(
    event_id: UUID,
    payload: RejectInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_REVIEW))],
) -> dict:
    """驳回候选事件，将驳回原因写入审计日志并提交状态。"""
    event = await get_event(session, event_id)
    await session.refresh(event, with_for_update=True)
    if payload.expected_version_id and payload.expected_version_id != event.current_version_id:
        raise ConflictError("event version changed")
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user,
        event.organization_unit_id,
        Permissions.EVENT_REVIEW,
        owner_id=event.created_by,
    )
    if event.business_status != "candidate":  # 确认和驳回仅适用于候选状态
        raise ConflictError("only candidate event can be rejected")
    event.business_status = "rejected"  # 将候选标记为人工驳回
    await add_audit(  # 记录人工审核动作及操作者，随业务事务提交
        session,
        user=user,
        action="production_event.reject",  # 步骤动作或取证决策
        resource_type="production_event",
        resource_id=event.id,
        reason=payload.reason,  # 驳回原因，供审计保存
    )
    await session.commit()  # 提交当前业务状态与关联记录
    return {"id": str(event.id), "business_status": event.business_status}


@router.post("/events/{event_id}:reextract", status_code=202)
async def reextract_event(
    event_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_TRANSFORM))],
) -> dict:
    """以事件当前描述重新发起抽取，保留原事件不变。"""
    event = await get_event(session, event_id)
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user,
        event.organization_unit_id,
        Permissions.EVENT_TRANSFORM,
        owner_id=event.created_by,
    )
    version = await session.get(ProductionEventVersion, event.current_version_id)
    if version is None:
        raise ConflictError("event current version is missing")
    payload = EventExtractionCreate(
        source_type="raw_text", raw_text=version.description
    )  # 基于当前描述创建新的持久化来源，不覆盖原事件
    job_id, events = await extract_events(
        session,
        payload=payload,
        user=user,
        provider=request.app.state.providers.text,
        inline=request.app.state.settings.run_tasks_inline,
    )
    return {"job_id": str(job_id), "event_ids": [str(item.id) for item in events]}


@router.post("/events:merge", status_code=201)
async def post_merge(
    payload: MergeEventsInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_TRANSFORM))],
) -> dict:
    """执行事件合并并返回新候选事件标识。"""
    event = await merge_events(session, payload, user)
    return {"id": str(event.id)}


@router.post("/events/{event_id}:split", status_code=201)
async def post_split(
    event_id: UUID,
    payload: SplitEventInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_TRANSFORM))],
) -> dict:
    """执行事件拆分并返回新候选事件标识列表。"""
    events = await split_event(session, await get_event(session, event_id), payload, user)
    return {"event_ids": [str(item.id) for item in events]}


@router.post("/events/{event_id}:classify", status_code=202)
async def post_classify(
    event_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_CLASSIFY))],
    options: InvestigationOptions | None = None,
) -> dict:
    """启动或复用事件调查，返回任务、调查运行及可用的评估标识。"""
    job_id, workflow, assessment = await classify_event(
        session,
        event=await get_event(session, event_id),
        user=user,
        provider=request.app.state.providers.text,
        inline=request.app.state.settings.run_tasks_inline,
        knowledge=request.app.state.knowledge,
        site_context=request.app.state.site_context,
        options=options,
    )
    agent_run = await session.scalar(select(AgentRun).where(AgentRun.job_id == job_id))
    return {
        "job_id": str(job_id),
        "agent_run_id": str(agent_run.id) if agent_run else None,
        "workflow_id": str(workflow.id) if workflow else None,
        "assessment_id": str(assessment.id) if assessment else None,
    }


@router.get("/event-extractions/{job_id}/results")
async def extraction_results(
    job_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_READ))],
):
    from app.shared.platform.models import JobEvent
    from app.shared.platform.service import get_job

    job = await get_job(session, job_id)
    if job.job_type != "event_extraction":
        raise ConflictError("job is not an event extraction")
    require_data_scope(
        user, job.organization_unit_id, Permissions.EVENT_READ, owner_id=job.requested_by
    )
    event = await session.scalar(
        select(JobEvent)
        .where(JobEvent.job_id == job.id, JobEvent.event_type == "succeeded")
        .order_by(JobEvent.created_at.desc())
        .limit(1)
    )
    ids = event.payload.get("result", {}).get("event_ids", []) if event else []
    for event_id in ids:
        record = await get_event(session, UUID(event_id))
        require_data_scope(
            user, record.organization_unit_id, Permissions.EVENT_READ, owner_id=record.created_by
        )
    return {"job_id": str(job.id), "status": job.status, "event_ids": ids}


@router.get("/investigation-skills")
async def investigation_skills(
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_CLASSIFY))],
):
    from app.modules.operation_event.application.skills import catalog

    return [skill.summary() for skill in catalog().values()]


@router.get("/agent-runs/{run_id}/input-request")
async def input_request(
    run_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_CLASSIFY))],
):
    from app.modules.operation_event.application.input_requests import read_request

    return await read_request(session, user, run_id)


@router.post("/agent-runs/{run_id}:resume", status_code=202)
async def resume_investigation(
    run_id: UUID,
    payload: ResumeInvestigation,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_CLASSIFY))],
):
    from app.modules.operation_event.application.input_requests import resume

    return await resume(
        session,
        user,
        run_id,
        payload,
        inline=request.app.state.settings.run_tasks_inline,
        provider=request.app.state.providers.text,
        knowledge=request.app.state.knowledge,
        site_context=request.app.state.site_context,
    )


def optimization_view(row, job):
    return {
        "id": str(row.id),
        "job_id": str(row.job_id),
        "status": job.status,
        "evaluation_id": str(row.evaluation_id),
        "options": row.options,
        "results": row.results,
        "candidate_id": str(row.candidate_id) if row.candidate_id else None,
        "published_improvement_id": str(row.published_improvement_id)
        if row.published_improvement_id
        else None,
    }


@router.post("/prompt-optimizations", status_code=202)
async def optimize_prompt(
    payload: OptimizationCreate,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_PROMPT))],
):
    from app.modules.operation_event.application.optimization import create
    from app.shared.platform.models import AsyncJob

    row = await create(
        session,
        user,
        payload,
        request.app.state.providers.text,
        inline=request.app.state.settings.run_tasks_inline,
    )
    return optimization_view(row, await session.get(AsyncJob, row.job_id))


@router.get("/prompt-optimizations/{optimization_id}")
async def read_optimization(
    optimization_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_PROMPT))],
):
    from app.modules.operation_event.application.optimization import get_optimization
    from app.shared.platform.models import AsyncJob

    row = await get_optimization(session, user, optimization_id)
    return optimization_view(row, await session.get(AsyncJob, row.job_id))


@router.post("/prompt-optimizations/{optimization_id}:publish")
async def publish_optimization(
    optimization_id: UUID,
    payload: PublishOptimization,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_PROMPT))],
):
    from app.modules.operation_event.application.optimization import publish
    from app.shared.platform.models import AsyncJob

    row = await publish(session, user, optimization_id, payload)
    return optimization_view(row, await session.get(AsyncJob, row.job_id))


@router.get("/events/{event_id}/site-context/{operation}")
async def event_site_context(
    event_id: UUID,
    operation: Literal[
        "get_weather_alerts", "get_map_context", "query_enterprise_gis", "get_access_route"
    ],
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.EVENT_READ))],
    station_id: UUID | None = None,
    origin_station_id: UUID | None = None,
):
    from app.modules.operation_event.application.site_context import investigate_site

    return await investigate_site(
        session,
        user,
        event_id,
        operation,
        request.app.state.site_context,
        station_id=station_id,
        origin_station_id=origin_station_id,
    )

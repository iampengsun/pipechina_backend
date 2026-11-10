"""Each investigation attempt owns a fresh set of agent tasks and evidence."""

from uuid import UUID, uuid4

from app.modules.operation_event.domain.agent_models import AgentRun
from app.modules.operation_event.domain.models import ProductionEvent
from app.shared.errors import ConflictError
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions


async def bind_classification_retry(session, original, retried, user):
    """校验原事件版本仍有效，为重试任务绑定全新的调查运行。"""
    event = await session.get(ProductionEvent, original.resource_id, with_for_update=True)
    if event is None or event.business_status != "confirmed":
        raise ConflictError("classification requires a confirmed event")
    require_data_scope(  # 校验当前操作对目标组织和所有者的数据范围
        user, event.organization_unit_id, Permissions.EVENT_CLASSIFY, owner_id=event.created_by
    )
    previous = await session.get(AgentRun, UUID(original.config_snapshot["agent_run_id"]))
    if previous is None or event.current_version_id != previous.event_version_id:
        raise ConflictError("event changed; start a new classification")
    from app.modules.operation_event.application.memory import observation_revision

    if previous.config_snapshot.get("observation_revision") != await observation_revision(
        session, event
    ):
        raise ConflictError("case observations changed; start a new classification")
    run = AgentRun(
        event_id=event.id,  # 关联生产事件标识
        event_version_id=previous.event_version_id,  # 本次调查固定的事件版本
        organization_unit_id=event.organization_unit_id,  # 所属组织单元，用于数据范围隔离
        job_id=retried.id,  # 关联异步任务标识
        status="queued",  # 当前执行状态
        current_stage="coordinator",  # 运行阶段标记
        created_by=user.user_id,  # 创建用户标识
        trace_id=uuid4(),  # 整次调查的链路标识
        config_snapshot={
            k: v for k, v in previous.config_snapshot.items() if k != "mcp_task"
        },  # 本次执行采用的配置快照
    )
    session.add(run)  # 将调查运行加入当前事务
    await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
    retried.config_snapshot = {
        **retried.config_snapshot,
        "agent_run_id": str(run.id),
    }  # 让重试绑定新运行，保留旧尝试的步骤和证据

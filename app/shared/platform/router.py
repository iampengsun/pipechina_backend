from __future__ import annotations

# 平台管理API：任务、事件流、模型配置、提示词、业务配置和调用日志。
import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.shared.db import SessionDep
from app.shared.errors import ConflictError, NotFoundError
from app.shared.platform.models import (
    AICallLog,
    BusinessConfigVersion,
    ModelAlias,
    PromptTemplateVersion,
)
from app.shared.platform.runtime import validate_prompt_schema
from app.shared.platform.schemas import (
    ActivationInput,
    BusinessConfigInput,
    JobView,
    ModelAliasInput,
    PromptTemplateInput,
)
from app.shared.platform.service import (
    add_audit,
    get_job,
    list_job_events,
    lock_configuration_key,
    request_cancel,
    retry_job,
)
from app.shared.security.authorization.dependencies import require_data_scope, require_permission
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser

# 创建任务相关接口路由。
# 最终接口会在主程序中统一添加API前缀，例如/api/v1/jobs。
jobs_router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
)

# 创建系统管理相关接口路由。
# 最终接口会在主程序中统一添加API前缀，例如/api/v1/admin。
admin_router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)


@jobs_router.get(
    "/{job_id}",
    response_model=JobView,
)
async def read_job(
    job_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.JOB_READ))],
) -> JobView:
    """
    查询指定异步任务的详细信息。

    函数首先根据job_id查询AIJob，然后检查当前用户是否可以访问
    该任务所属组织的数据。权限检查通过后，将SQLAlchemy任务对象
    转换成JobView并返回给客户端。
    """
    job = await get_job(  # 根据任务ID查询AIJob，不存在时抛出404异常。
        session,
        job_id,
    )

    require_data_scope(  # 检查当前用户是否有权访问任务所属组织的数据。
        user,
        job.organization_unit_id,
        Permissions.JOB_READ,
        owner_id=job.requested_by,
    )

    return JobView.model_validate(job)  # 将SQLAlchemy对象转换成Pydantic响应模型。


@jobs_router.get("/{job_id}/events")
async def read_job_events(
    job_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.JOB_READ))],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
):
    """
    以SSE形式返回指定任务的事件时间线。

    函数先查询任务并执行组织数据范围检查，然后按照事件创建顺序
    查询JobEvent。内部异步生成器会把每条事件转换成SSE文本格式，
    最后通过StreamingResponse返回。

    当前实现会先一次性查询已有事件，再逐条发送。因此它返回的是
    当前已有事件的流式快照，不会持续等待数据库中新产生的事件。
    """
    job = await get_job(  # 查询事件所属的异步任务。
        session,
        job_id,
    )

    require_data_scope(  # 检查当前用户是否可以访问该任务所属组织。
        user,
        job.organization_unit_id,
        Permissions.JOB_READ,
        owner_id=job.requested_by,
    )

    async def stream():
        """
        将任务事件转换成Server-Sent Events文本。

        每条SSE消息包含事件ID、事件类型以及JSON格式的事件数据。
        空行表示一条SSE消息结束。
        """
        cursor: UUID | None = None
        if last_event_id:
            try:
                cursor = UUID(last_event_id)
            except ValueError:
                yield 'event: error\ndata: {"code":"INVALID_LAST_EVENT_ID"}\n\n'
                return
        idle_ticks = 0
        while not await request.is_disconnected():
            events = await list_job_events(session, job_id, after_event_id=cursor)
            for event in events:
                cursor = event.id
                payload = {"type": event.event_type, "data": event.payload}
                yield (
                    f"id: {event.id}\nevent: {event.event_type}\ndata: {json.dumps(payload)}\n\n"
                )
            await session.rollback()
            current = await session.get(type(job), job_id)
            terminal = current is None or current.status in {"succeeded", "failed", "cancelled"}
            if terminal and not events:
                return
            idle_ticks += 1
            if idle_ticks % 15 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(  # 返回流式HTTP响应。
        stream(),  # 把异步事件生成器作为响应内容。
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@jobs_router.get("/{job_id}/event-history")
async def read_job_event_history(
    job_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.JOB_READ))],
    after_event_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """返回指定任务的状态事件历史。"""

    job = await get_job(session, job_id)
    require_data_scope(
        user, job.organization_unit_id, Permissions.JOB_READ, owner_id=job.requested_by
    )
    rows = await list_job_events(session, job_id, after_event_id=after_event_id, limit=limit)
    return [
        {
            "id": str(row.id),
            "event_type": row.event_type,
            "payload": row.payload,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@jobs_router.get("/{job_id}/ai-calls")
async def read_job_ai_calls(
    job_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.JOB_READ))],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """返回指定任务关联的模型调用记录。"""

    job = await get_job(session, job_id)
    require_data_scope(
        user, job.organization_unit_id, Permissions.JOB_READ, owner_id=job.requested_by
    )
    rows = await session.scalars(
        select(AICallLog)
        .where(AICallLog.job_id == job_id)
        .order_by(AICallLog.created_at)
        .limit(limit)
    )
    return [
        {
            "id": str(row.id),
            "provider": row.provider,
            "model_alias": row.model_alias,
            "model_name": row.model_name,
            "provider_request_id": row.provider_request_id,
            "duration_ms": row.duration_ms,
            "input_units": row.input_units,
            "output_units": row.output_units,
            "status": row.status,
            "error_code": row.error_code,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@jobs_router.post(
    "/{job_id}:cancel",
    response_model=JobView,
)
async def cancel_job(
    job_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.JOB_CANCEL))],
) -> JobView:
    """
    请求取消指定的异步任务。

    函数先查询任务并检查组织数据范围，然后调用request_cancel()
    设置cancel_requested标记。该操作属于协作式取消，Worker需要主动
    检查取消标记并停止后续处理。

    取消请求还会写入审计日志，最后与任务状态修改一起提交。
    """
    job = await get_job(  # 根据任务ID查询异步任务。
        session,
        job_id,
    )

    require_data_scope(  # 检查用户能否操作该任务所属组织的数据。
        user,
        job.organization_unit_id,
        Permissions.JOB_CANCEL,
        owner_id=job.requested_by,
    )

    await request_cancel(  # 设置任务的取消请求标记并追加任务事件。
        session,
        job,
    )

    await request.app.state.synchronize_cancelled_job(session, job)

    await add_audit(  # 创建任务取消请求审计日志。
        session,
        user=user,  # 记录发起取消请求的用户。
        action="job.cancel_requested",  # 记录本次操作名称。
        resource_type="job",  # 标记被操作的资源类型为任务。
        resource_id=job.id,  # 记录被取消任务的ID。
    )

    await session.commit()  # 在同一事务中提交取消标记、任务事件和审计日志。

    await session.refresh(job)
    return JobView.model_validate(job)  # 返回更新后的任务信息。


@jobs_router.post(
    "/{job_id}:retry",
    response_model=JobView,
    status_code=202,
)
async def retry(
    job_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.JOB_RETRY))],
) -> JobView:
    """
    为失败或已取消的异步任务创建重试任务。

    函数先查询原任务并检查组织数据范围，然后调用retry_job()
    创建新的AIJob、JobEvent和TaskOutbox记录。

    接口返回202表示重试任务已经创建并等待异步处理，
    不代表具体业务任务已经执行完成。
    """
    original = await get_job(  # 查询需要重试的原始异步任务。
        session,
        job_id,
    )

    require_data_scope(  # 检查用户是否有权操作原任务所属组织的数据。
        user,
        original.organization_unit_id,
        Permissions.JOB_RETRY,
        owner_id=original.requested_by,
    )

    job = await retry_job(  # 根据原任务创建新的重试任务。
        session,
        original,
        user,
        on_retry=request.app.state.job_retry_handlers.get(original.job_type),
    )

    await session.commit()  # 提交新任务、任务事件和Outbox记录。
    await session.refresh(job)

    return JobView.model_validate(job)  # 返回新创建的重试任务。


@admin_router.get("/model-aliases")
async def list_model_aliases(
    session: SessionDep,
    _: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.ADMIN_MODEL)),
    ],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    provider: str | None = None,
    enabled: bool | None = None,
) -> list[dict]:
    """
    查询系统中的全部模型别名。

    只有拥有admin:model权限的用户可以访问。查询结果按照模型别名
    code排序，并返回模型供应商、实际模型名称、模型快照、启用状态
    和附加配置。

    参数名使用下划线表示接口只需要权限依赖执行，不需要在函数体中
    使用返回的CurrentUser对象。
    """
    statement = select(ModelAlias)
    if provider is not None:
        statement = statement.where(ModelAlias.provider == provider)
    if enabled is not None:
        statement = statement.where(ModelAlias.enabled.is_(enabled))
    rows = await session.scalars(statement.order_by(ModelAlias.code).offset(offset).limit(limit))

    return [  # 将ModelAlias ORM对象转换成可以JSON序列化的字典。
        {
            "id": str(row.id),  # 返回模型别名记录ID。
            "code": row.code,  # 返回业务使用的模型别名。
            "provider": row.provider,  # 返回模型服务供应商。
            "model_name": row.model_name,  # 返回供应商侧实际模型名称。
            "capability": row.capability,
            "model_snapshot": row.model_snapshot,  # 返回固定模型版本或快照。
            "enabled": row.enabled,  # 返回该模型别名是否启用。
            "config": row.config,  # 返回温度、最大Token数等附加配置。
        }
        for row in rows  # 遍历查询得到的所有模型别名。
    ]


@admin_router.put("/model-aliases/{code}")
async def put_model_alias(
    code: str,
    payload: ModelAliasInput,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.ADMIN_MODEL)),
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict:
    """
    新增或更新一个模型别名。

    接口根据URL中的code查询ModelAlias。如果记录不存在则创建，
    如果已经存在则更新原记录，因此该接口同时支持新增和修改。

    修改完成后会写入审计日志，并在同一个事务中提交模型配置和
    审计记录。
    """
    alias = await session.scalar(  # 根据模型别名code查询现有记录。
        select(ModelAlias).where(ModelAlias.code == code)
    )

    if alias is None:  # 如果该模型别名尚不存在。
        alias = ModelAlias(code=code)  # 创建新的模型别名ORM对象。

        session.add(alias)  # 将新对象加入当前数据库Session。
    elif if_match is not None and if_match.strip('"') != alias.updated_at.isoformat():
        raise ConflictError(
            "model alias was changed by another administrator",
            current_etag=alias.updated_at.isoformat(),
        )

    alias.provider = payload.provider  # 更新模型服务供应商。

    alias.model_name = payload.model_name  # 更新实际模型名称。
    alias.capability = payload.capability

    alias.model_snapshot = payload.model_snapshot  # 更新模型快照或版本。

    alias.enabled = payload.enabled  # 更新模型别名启用状态。

    alias.config = payload.config  # 更新模型附加配置。

    await session.flush()  # 把变更发送到数据库并确保新记录已经获得ID。

    await add_audit(  # 创建模型别名新增或修改的审计日志。
        session,
        user=user,  # 记录执行操作的当前用户。
        action="model_alias.put",  # 记录操作名称。
        resource_type="model_alias",  # 标记资源类型为模型别名。
        resource_id=alias.id,  # 记录被修改的模型别名ID。
        after=payload.model_dump(),  # 保存修改后的请求数据快照。
    )

    await session.commit()  # 提交模型别名修改和审计日志。

    return {  # 返回模型别名的基本标识信息。
        "id": str(alias.id),  # 返回模型别名记录ID。
        "code": alias.code,  # 返回模型别名code。
        "etag": alias.updated_at.isoformat(),
    }


@admin_router.get("/prompt-templates")
async def list_prompt_templates(
    session: SessionDep,
    _: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.ADMIN_PROMPT)),
    ],
    template_code: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """
    查询全部提示词模板版本。

    只有拥有admin:prompt权限的用户可以访问。结果首先按照
    template_code排序，同一个模板内部按照version倒序排列，
    因此最新版本会出现在较前位置。
    """
    statement = select(PromptTemplateVersion)
    if template_code is not None:
        statement = statement.where(PromptTemplateVersion.template_code == template_code)
    if active is not None:
        statement = statement.where(PromptTemplateVersion.active.is_(active))
    rows = await session.scalars(
        statement.order_by(
            PromptTemplateVersion.template_code,  # 首先按照模板编号排序。
            PromptTemplateVersion.version.desc(),  # 同一模板按照版本号倒序排列。
        )
        .offset(offset)
        .limit(limit)
    )

    return [  # 将提示词模板ORM对象转换成字典。
        {
            "id": str(row.id),  # 返回模板版本记录ID。
            "template_code": row.template_code,  # 返回模板业务编号。
            "version": row.version,  # 返回模板版本号。
            "active": row.active,  # 返回该版本当前是否激活。
        }
        for row in rows  # 遍历全部模板版本。
    ]


@admin_router.post(
    "/prompt-templates",
    status_code=201,
)
async def create_prompt_template(
    payload: PromptTemplateInput,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.ADMIN_PROMPT)),
    ],
) -> dict:
    """
    创建一个新的提示词模板版本。

    函数查询指定template_code当前的最大版本号，然后在最大版本号
    基础上加1。新创建的版本默认不激活，需要通过后续业务流程完成
    检查和激活。
    """
    await lock_configuration_key(session, f"prompt:{payload.template_code}")
    max_version = await session.scalar(
        select(func.max(PromptTemplateVersion.version)).where(
            PromptTemplateVersion.template_code == payload.template_code
        )
    )

    row = PromptTemplateVersion(  # 创建新的提示词模板版本记录。
        **payload.model_dump(),  # 写入模板编号、系统提示词、用户模板和输出Schema。
        version=(max_version or 0) + 1,  # 没有历史版本时从1开始，否则最大版本加1。
        active=False,  # 新版本默认不激活。
        created_by=user.user_id,  # 记录创建该版本的用户ID。
    )

    session.add(row)  # 将新模板版本加入数据库Session。

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("prompt version was created concurrently; retry the request") from exc

    return {  # 返回新版本的标识信息。
        "id": str(row.id),  # 返回新模板版本记录ID。
        "version": row.version,  # 返回新生成的版本号。
    }


@admin_router.get("/business-configs")
async def list_business_configs(
    session: SessionDep,
    _: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.ADMIN_CONFIG)),
    ],
    config_code: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """
    查询全部业务配置版本。

    只有拥有admin:config权限的用户可以访问。结果首先按照
    config_code排序，同一配置编号内部按照版本号倒序排列。
    """
    statement = select(BusinessConfigVersion)
    if config_code is not None:
        statement = statement.where(BusinessConfigVersion.config_code == config_code)
    if active is not None:
        statement = statement.where(BusinessConfigVersion.active.is_(active))
    rows = await session.scalars(
        statement.order_by(
            BusinessConfigVersion.config_code,  # 首先按照配置业务编号排序。
            BusinessConfigVersion.version.desc(),  # 同一配置按照版本号倒序排列。
        )
        .offset(offset)
        .limit(limit)
    )

    return [  # 将业务配置ORM对象转换成响应字典。
        {
            "id": str(row.id),  # 返回业务配置版本记录ID。
            "config_code": row.config_code,  # 返回配置业务编号。
            "config_type": row.config_type,  # 返回配置类型。
            "version": row.version,  # 返回配置版本号。
            "active": row.active,  # 返回该版本当前是否激活。
            "payload": row.payload,  # 返回该版本的完整配置内容。
        }
        for row in rows  # 遍历查询到的全部业务配置版本。
    ]


@admin_router.post(
    "/business-configs",
    status_code=201,
)
async def create_business_config(
    payload: BusinessConfigInput,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.ADMIN_CONFIG)),
    ],
) -> dict:
    """
    创建一个新的业务配置版本。

    函数查询同一config_code当前的最大版本号，然后创建下一个版本。
    新版本默认不激活，避免未经确认的配置立即影响正式业务流程。
    """
    await lock_configuration_key(session, f"config:{payload.config_code}")
    max_version = await session.scalar(
        select(func.max(BusinessConfigVersion.version)).where(
            BusinessConfigVersion.config_code == payload.config_code
        )
    )

    row = BusinessConfigVersion(  # 创建新的业务配置版本记录。
        **payload.model_dump(),  # 写入配置编号、配置类型和配置内容。
        version=(max_version or 0) + 1,  # 计算新配置版本号。
        active=False,  # 新配置版本默认不激活。
        created_by=user.user_id,  # 记录创建配置版本的用户ID。
    )

    session.add(row)  # 将新配置版本加入数据库Session。

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("business config version was created concurrently; retry") from exc

    return {  # 返回新配置版本的标识信息。
        "id": str(row.id),  # 返回新配置版本记录ID。
        "version": row.version,  # 返回新生成的版本号。
    }


@admin_router.post("/business-configs/{config_id}:activate")
async def activate_business_config(
    config_id: UUID,
    payload: ActivationInput,
    session: SessionDep,
    user: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.ADMIN_CONFIG)),
    ],
) -> dict:
    """
    激活指定的业务配置版本。

    函数先查询目标版本，然后找出同一config_code下当前已经激活的
    所有版本并将它们停用，最后把目标版本设置为激活。

    该操作保证同一个config_code在正常情况下只有一个激活版本。
    激活操作还会写入审计日志。
    """
    row = await session.get(  # 根据主键查询需要激活的业务配置版本。
        BusinessConfigVersion,
        config_id,
    )

    if row is None:  # 如果指定的业务配置版本不存在。
        raise NotFoundError(  # 抛出资源不存在异常。
            "business_config",
            config_id,
        )

    await lock_configuration_key(session, f"config:{row.config_code}")
    await session.execute(
        update(BusinessConfigVersion)
        .where(
            BusinessConfigVersion.config_code == row.config_code,  # 限制为相同配置编号。
            BusinessConfigVersion.active.is_(True),  # 只查询当前激活的版本。
        )
        .values(active=False)
    )
    await session.flush()
    row.active = True  # 将目标配置版本设置为激活。

    await add_audit(  # 创建业务配置激活审计日志。
        session,
        user=user,  # 记录执行激活操作的用户。
        action="business_config.activate",  # 记录操作名称。
        resource_type="business_config",  # 标记资源类型为业务配置。
        resource_id=row.id,  # 记录被激活的配置版本ID。
        reason=payload.reason,
    )

    await session.commit()  # 提交旧版本停用、目标版本激活和审计日志。

    return {  # 返回配置激活结果。
        "id": str(row.id),  # 返回被激活的配置版本ID。
        "active": True,  # 明确表示该版本已经激活。
    }


@admin_router.get("/model-aliases/{code}")
async def get_model_alias_detail(
    code: str,
    session: SessionDep,
    _: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_MODEL))],
) -> dict:
    """按业务代码读取模型别名详情。"""

    row = await session.scalar(select(ModelAlias).where(ModelAlias.code == code))
    if row is None:
        raise NotFoundError("model_alias", code)
    return {
        "id": str(row.id),
        "code": row.code,
        "provider": row.provider,
        "model_name": row.model_name,
        "capability": row.capability,
        "model_snapshot": row.model_snapshot,
        "enabled": row.enabled,
        "config": row.config,
        "updated_at": row.updated_at.isoformat(),
    }


@admin_router.get("/prompt-templates/{template_code}/versions/{version}")
async def get_prompt_template_version(
    template_code: str,
    version: int,
    session: SessionDep,
    _: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_PROMPT))],
) -> dict:
    """读取指定编号和版本的提示词模板。"""

    row = await session.scalar(
        select(PromptTemplateVersion).where(
            PromptTemplateVersion.template_code == template_code,
            PromptTemplateVersion.version == version,
        )
    )
    if row is None:
        raise NotFoundError("prompt_template", f"{template_code}:{version}")
    return {
        "id": str(row.id),
        "template_code": row.template_code,
        "version": row.version,
        "system_prompt": row.system_prompt,
        "user_template": row.user_template,
        "output_schema": row.output_schema,
        "active": row.active,
        "created_at": row.created_at.isoformat(),
    }


@admin_router.get("/prompt-templates/{template_code}/diff")
async def diff_prompt_template_versions(
    template_code: str,
    session: SessionDep,
    _: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_PROMPT))],
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
) -> dict:
    """比较两个提示词模板版本的字段变化。"""

    rows = list(
        await session.scalars(
            select(PromptTemplateVersion).where(
                PromptTemplateVersion.template_code == template_code,
                PromptTemplateVersion.version.in_((from_version, to_version)),
            )
        )
    )
    by_version = {row.version: row for row in rows}
    if from_version not in by_version or to_version not in by_version:
        raise NotFoundError("prompt_template_diff", template_code)
    before, after = by_version[from_version], by_version[to_version]
    return {
        "template_code": template_code,
        "from_version": from_version,
        "to_version": to_version,
        "changes": {
            "system_prompt": before.system_prompt != after.system_prompt,
            "user_template": before.user_template != after.user_template,
            "output_schema": before.output_schema != after.output_schema,
        },
    }


@admin_router.post("/prompt-templates/{template_id}:activate")
async def activate_prompt_template(
    template_id: UUID,
    request: Request,
    payload: ActivationInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_PROMPT))],
) -> dict:
    """停用旧版本并激活目标提示词模板。"""

    row = await session.get(PromptTemplateVersion, template_id)
    if row is None:
        raise NotFoundError("prompt_template", template_id)
    await request.app.state.validate_prompt_activation(session, template_id)
    validate_prompt_schema(row.template_code, row.output_schema)
    await lock_configuration_key(session, f"prompt:{row.template_code}")
    await session.execute(
        update(PromptTemplateVersion)
        .where(
            PromptTemplateVersion.template_code == row.template_code,
            PromptTemplateVersion.active.is_(True),
        )
        .values(active=False)
    )
    await session.flush()
    row.active = True
    await add_audit(
        session,
        user=user,
        action="prompt_template.activate",
        resource_type="prompt_template",
        resource_id=row.id,
        reason=payload.reason,
    )
    await session.commit()
    return {"id": str(row.id), "version": row.version, "active": True}


@admin_router.get("/business-configs/{config_code}/versions/{version}")
async def get_business_config_version(
    config_code: str,
    version: int,
    session: SessionDep,
    _: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_CONFIG))],
) -> dict:
    """读取指定编号和版本的业务配置。"""

    row = await session.scalar(
        select(BusinessConfigVersion).where(
            BusinessConfigVersion.config_code == config_code,
            BusinessConfigVersion.version == version,
        )
    )
    if row is None:
        raise NotFoundError("business_config", f"{config_code}:{version}")
    return {
        "id": str(row.id),
        "config_code": row.config_code,
        "config_type": row.config_type,
        "version": row.version,
        "payload": row.payload,
        "active": row.active,
        "created_at": row.created_at.isoformat(),
    }

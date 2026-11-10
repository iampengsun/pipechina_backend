"""In-process, read-only investigation tools (not an MCP transport)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.knowledge import KnowledgeResourceManager
from app.modules.handover.domain.models import ManualOperationRecord, ManualOperationRecordVersion
from app.modules.knowledge.application.retrieval import retrieve
from app.modules.knowledge.domain.schemas import SearchInput
from app.modules.maintenance_order.domain.models import WorkOrder
from app.modules.operation_event.application.context import current_evidence
from app.modules.operation_event.domain.agent_models import (
    AgentEvidence,
    AgentProposal,
    AgentRun,
    AgentTask,
)
from app.modules.operation_event.domain.models import ProductionEvent, ProductionEventVersion
from app.ports.text import TextLLMProvider
from app.shared.errors import ConflictError
from app.shared.security.authorization.dependencies import data_scope_clause
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser

InvestigationHandler = Callable[
    [str], Awaitable[list[dict]]
]  # 接收查询词并异步返回业务记录的处理函数类型


@dataclass(frozen=True)
class InvestigationTool:
    """声明一个调查工具的来源标签、名称、只读属性和处理函数。"""

    server: str  # 工具来源标签
    name: str  # 工具白名单名称
    read_only: bool  # 只读标记，注册时拒绝写工具
    handler: InvestigationHandler  # 服务端绑定的异步处理函数
    description: str = ""


class InvestigationToolRegistry:
    """维护进程内只读工具白名单，并将工具返回内容保存为调查证据。"""

    def __init__(self) -> None:
        """初始化进程内调查工具映射。"""
        self.text_provider: TextLLMProvider | None = None
        self.active_text: TextLLMProvider | None = None
        self.site_context = None
        self.knowledge: KnowledgeResourceManager | None = None
        self._tools: dict[str, InvestigationTool] = {}  # 保存仅由服务端注册的工具映射

    def register(self, tool: InvestigationTool) -> None:
        """只允许注册只读工具，并按工具名保存处理函数。"""
        if not tool.read_only:  # 注册阶段拒绝具有写入能力的工具
            raise ConflictError("write investigation tools require an approval capability")
        self._tools[tool.name] = tool  # 以工具名注册，同名注册会替换原处理函数

    def list_tools(self) -> list[dict]:
        """返回可向模型公开的工具元信息，不暴露处理函数或数据库连接。"""
        return [
            {
                "server": item.server,
                "name": item.name,
                "read_only": item.read_only,
                "description": item.description,
            }
            for item in self._tools.values()
        ]

    async def call(
        self,
        session: AsyncSession,
        *,
        run: AgentRun,
        task: AgentTask,
        tool_name: str,
        query: str,
    ) -> list[AgentEvidence]:
        """校验工具白名单，执行限量查询并保存带哈希的证据快照。"""
        tool = self._tools.get(tool_name)
        if tool is None or not tool.read_only:  # 调用阶段再次校验工具白名单与只读属性
            raise ConflictError("investigation tool is not allowed", tool=tool_name)
        if self.text_provider is not None:
            from app.modules.operation_event.application.investigation import _call
            from app.shared.platform.models import AsyncJob

            job = await session.get(AsyncJob, run.job_id)
            if job is None:
                raise ConflictError("investigation job is unavailable")
            active_job = job
            underlying = self.text_provider

            class BudgetedText:
                name, model = underlying.name, underlying.model

                async def generate_structured(self, **kwargs):
                    return await _call(
                        session,
                        job=active_job,
                        run=run,
                        task=task,
                        provider=underlying,
                        operation=kwargs["operation"],
                        model=kwargs["response_model"],
                        payload={},
                        system_prompt_override=kwargs["system_prompt"],
                        user_prompt_override=kwargs["user_prompt"],
                    )

            self.active_text = BudgetedText()
        rows = await tool.handler(query[:100])  # 限制查询词长度后调用服务端绑定的函数
        evidence: list[AgentEvidence] = []
        for row in rows[:5]:  # 每次工具调用最多保存五条证据
            encoded = json.dumps(
                row, ensure_ascii=False, sort_keys=True
            ).encode()  # 稳定排序后编码，使同内容得到一致哈希
            item = AgentEvidence(
                run_id=run.id,  # 所属调查运行标识
                task_id=task.id,  # 所属角色任务标识
                source_server=tool.server,  # 证据来源标签，不表示 MCP 服务
                source_resource=str(row.get("record_id") or row.get("id")),  # 来源资源标识
                source_version=str(
                    row.get("version_id") or row.get("version") or ""
                ),  # 证据对应的来源版本
                content=row,  # 取证时保存的内容快照
                content_hash=hashlib.sha256(encoded).hexdigest(),  # 内容摘要，用于核对证据内容
                access_scope={
                    "organization_unit_id": str(run.organization_unit_id)
                },  # 取证时记录的访问范围
            )
            session.add(item)  # 将当前记录加入当前事务
            evidence.append(item)  # 收集本次保存的证据，供调查循环累积
        await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
        return evidence  # 返回已持久化的工具证据


async def _read_business_tool(
    session: AsyncSession,
    user: CurrentUser,
    event: ProductionEvent,
    action: str,
    query: str,
) -> list[dict]:
    """按查询类型读取同组织且在用户权限范围内的业务证据。"""
    model: Any
    if action == "search_events":
        model, permission = ProductionEvent, Permissions.EVENT_READ
        statement = select(model).where(
            model.business_status == "confirmed",  # 历史事件证据仅取已确认记录
            model.id != event.id,  # 排除当前事件，其固定版本已作为初始证据
            model.title.contains(
                query, autoescape=True
            ),  # 按标题匹配并转义通配符，避免扩大检索范围
        )
    elif action == "search_work_orders":
        model, permission = WorkOrder, Permissions.MAINTENANCE_READ
        statement = select(model).where(model.title.contains(query, autoescape=True))
    elif action == "search_handover":
        model, permission = ManualOperationRecord, Permissions.HANDOVER_READ
        statement = (
            select(model)
            .join(
                ManualOperationRecordVersion,
                ManualOperationRecordVersion.id == model.current_version_id,
            )
            .where(ManualOperationRecordVersion.content.contains(query, autoescape=True))
        )
    else:
        raise ConflictError("unknown investigation tool")
    statement = (
        statement.where(
            model.organization_unit_id
            == event.organization_unit_id,  # 工具查询始终限定在目标事件所属组织
            data_scope_clause(  # 在 SQL 中约束用户可见的数据范围
                user,
                permission,
                model.organization_unit_id,
                owner_column=model.created_by,
                assignee_column=WorkOrder.assignee_id if model is WorkOrder else None,
            ),
        )
        .order_by(model.created_at.desc(), model.id)  # 按创建时间取最新记录，并用标识稳定排序
        .limit(5)  # 数据库层最多取五条业务记录
    )
    observations: list[dict] = []
    for row in (await session.scalars(statement)).all():
        item = {"id": f"{action}:{row.id}", "record_id": str(row.id)}
        if isinstance(row, ProductionEvent):
            version = await session.get(ProductionEventVersion, row.current_version_id)
            item.update(
                title=row.title,  # 事件或条目的标题
                status=row.business_status,  # 来源事件的业务状态
                version_id=str(row.current_version_id),
                text=version.description[:2000]
                if version
                else "",  # 截断历史事件正文以控制模型上下文
            )
        elif isinstance(row, WorkOrder):
            item.update(
                title=row.title,  # 事件或条目的标题
                status=row.status,  # 来源工单的流转状态
                text=row.description[:2000],  # 截断工单描述以控制模型上下文
                version=str(row.version),  # 所属记录内的版本号
                event_id=str(row.event_id),  # 关联生产事件标识
            )
        else:
            manual_version = await session.get(ManualOperationRecordVersion, row.current_version_id)
            item.update(
                version_id=str(row.current_version_id),
                text=manual_version.content[:2000]
                if manual_version
                else "",  # 截断人工记录正文，缺少版本时返回空串
            )
        observations.append(item)  # 汇总带来源标识的业务内容
    return observations  # 返回已过滤并截断的业务证据


def build_investigation_tools(
    session: AsyncSession,
    user: CurrentUser,
    event: ProductionEvent,
    *,
    knowledge: KnowledgeResourceManager | None = None,
    text: TextLLMProvider | None = None,
    refresh_user: Callable[[], Awaitable[CurrentUser]] | None = None,
    run_id=None,
    site_context=None,
) -> InvestigationToolRegistry:
    """为当前用户和目标事件绑定三个只读业务查询工具。"""
    registry = InvestigationToolRegistry()
    registry.site_context = site_context
    registry.knowledge = knowledge
    registry.text_provider = text
    mapping = {
        "search_events": "event-tools",
        "search_work_orders": "work-order-tools",
        "search_handover": "handover-tools",
    }
    for name, server in mapping.items():

        async def handler(query: str, action: str = name) -> list[dict]:
            """使用创建闭包时绑定的动作名称查询证据，避免循环变量串用。"""
            return await _read_business_tool(session, user, event, action, query)

        registry.register(
            InvestigationTool(server=server, name=name, read_only=True, handler=handler)
        )
    if (
        knowledge is not None
        and text is not None
        and user.has_permission(Permissions.KNOWLEDGE_READ)
    ):

        async def search_knowledge(query: str) -> list[dict]:
            current = await refresh_user() if refresh_user else user
            evidence, trace = await retrieve(
                session,
                current,
                SearchInput(question=query, limit=5),
                knowledge.get(),
                registry.active_text or text,
            )
            return [{**row, "retrieval_trace": trace} for row in evidence]

        registry.register(
            InvestigationTool(
                server="knowledge-rag",
                name="search_knowledge",
                read_only=True,
                handler=search_knowledge,
                description="检索已发布、在有效期内且当前用户可见的规范、手册和案例；需要处置依据时调用，返回原文、版本与定位信息。",
            )
        )
    if user.has_permission(Permissions.MAINTENANCE_READ):

        async def search_experiences(query: str) -> list[dict]:
            from app.modules.operation_event.application.memory import search_experiences as search

            current = await refresh_user() if refresh_user else user
            return await search(session, current, event, query)

        registry.register(
            InvestigationTool(
                server="experience-memory",
                name="search_experiences",
                read_only=True,
                handler=search_experiences,
                description="检索同组织已审核、未过期的历史处置经验。经验只有在设备、现象、适用前提匹配时可参考，不能替代当前证据。",
            )
        )
    if knowledge is not None and user.has_permission(Permissions.GRAPH_READ):
        from app.modules.operational_graph.application.investigation import investigate_graph

        for name, description in (
            ("trace_case_relations", "追溯当前事件的交接班、隐患、工单、验收和知识引用路径。"),
            (
                "find_recurrent_hazards",
                "按人工确认的设备及故障类型，检查重复异常与整改验收后的再次登记。",
            ),
            (
                "assess_change_impact",
                "检查当前事件所引用知识的撤回、到期或新版发布影响，只提示人工复核。",
            ),
        ):

            async def graph_handler(query: str, operation=name):
                current = await refresh_user() if refresh_user else user
                return await investigate_graph(session, current, knowledge, event.id, operation)

            registry.register(
                InvestigationTool(
                    server="business-graph",
                    name=name,
                    read_only=True,
                    handler=graph_handler,
                    description=description,
                )
            )
    if user.has_permission(Permissions.MAINTENANCE_READ):

        async def verify_outcomes(query: str):
            from app.modules.maintenance_order.application.verification import verification_context

            current = await refresh_user() if refresh_user else user
            rows = await session.scalars(
                select(WorkOrder)
                .where(
                    WorkOrder.event_id == event.id,
                    data_scope_clause(
                        current,
                        Permissions.MAINTENANCE_READ,
                        WorkOrder.organization_unit_id,
                        owner_column=WorkOrder.created_by,
                        assignee_column=WorkOrder.assignee_id,
                    ),
                )
                .order_by(WorkOrder.created_at.desc())
                .limit(5)
            )
            result = []
            for order in rows:
                value = await verification_context(session, current, order.id)
                result.append({**value, "record_id": str(order.id), "version": order.version})
            return result

        registry.register(
            InvestigationTool(
                server="work-order-tools",
                name="verify_work_order_outcomes",
                read_only=True,
                handler=verify_outcomes,
                description="核查当前事件相关工单的措施、前后实测和独立验收，不凭关闭状态推断达标。",
            )
        )
    if run_id is not None:

        async def read_evidence(query: str):
            from uuid import UUID

            from app.shared.errors import NotFoundError

            identifier, _, offset = query.partition(":")
            start = int(offset or 0)
            if start < 0 or start > 1000000:
                raise ConflictError("invalid evidence offset")
            item = await session.get(AgentEvidence, UUID(identifier), populate_existing=True)
            if item is None or item.run_id != run_id:
                raise NotFoundError("investigation_evidence", identifier)
            current = await refresh_user() if refresh_user else user
            await authorize_knowledge_evidence(session, current, run_id)
            if item.source_server == "site-context":
                from app.modules.operation_event.application.site_context import (
                    validate_saved_context,
                )

                snapshots = list(
                    await session.scalars(
                        select(AgentEvidence).where(
                            AgentEvidence.run_id == run_id,
                            AgentEvidence.source_server == "site-context",
                        )
                    )
                )
                if item.id not in {row.id for row in current_evidence(snapshots)}:
                    raise ConflictError(
                        "external evidence is expired or superseded; query the tool again"
                    )
                await validate_saved_context(session, current, item.content, require_fresh=True)
            encoded = json.dumps(item.content, ensure_ascii=False, sort_keys=True)
            return [
                {
                    "record_id": str(item.id),
                    "version": item.content_hash,
                    "text": encoded[start : start + 6000],
                    "offset": start,
                    "next_offset": min(len(encoded), start + 6000),
                    "total_chars": len(encoded),
                    "complete": start + 6000 >= len(encoded),
                }
            ]

        registry.register(
            InvestigationTool(
                server="evidence-store",
                name="read_investigation_evidence",
                read_only=True,
                handler=read_evidence,
                description="按证据UUID:字符偏移分段回查本次调查已保存的完整来源，重新核对当前权限。",
            )
        )
    if user.has_permission(Permissions.EQUIPMENT_READ):
        from uuid import UUID

        from app.modules.operation_event.application.site_context import investigate_site

        for name, description in (
            (
                "get_weather_alerts",
                "查询事件关联站场当前气象预警；query可指定关联站场UUID，缺坐标或密钥时返回缺项，不能解释为无风险。",
            ),
            ("get_map_context", "读取事件站场地址、周边设施和道路参考，query可指定关联站场UUID。"),
            (
                "query_enterprise_gis",
                "按当前组织图层查询事件站场周边企业GIS要素，query可指定关联站场UUID。",
            ),
            (
                "get_access_route",
                (
                    "查询普通到场路线参考，query为出发站场UUID或 "
                    "出发站场UUID,目标站场UUID；不生成安全疏散路线。"
                ),
            ),
        ):

            async def context_handler(query, operation=name):
                current = await refresh_user() if refresh_user else user
                try:
                    values = [UUID(p.strip()) for p in query.split(",") if p.strip()]
                except ValueError:
                    values = []
                if len(values) > (2 if operation == "get_access_route" else 1):
                    raise ConflictError("invalid station selection")
                origin = values[0] if values and operation == "get_access_route" else None
                target = (
                    (values[1] if len(values) == 2 else None)
                    if operation == "get_access_route"
                    else (values[0] if values else None)
                )
                return await investigate_site(
                    session,
                    current,
                    event.id,
                    operation,
                    site_context,
                    station_id=target,
                    origin_station_id=origin,
                )

            registry.register(
                InvestigationTool(
                    server="site-context",
                    name=name,
                    read_only=True,
                    handler=context_handler,
                    description=description,
                )
            )
    return registry  # 返回绑定当前用户与事件的工具注册表


async def authorize_knowledge_evidence(
    session, user, run_id, *, check_memories=True, require_current_context=False
):
    """Recheck knowledge ACLs when reading stored investigation snapshots."""
    from uuid import UUID

    from app.modules.knowledge.application.access import version_access
    from app.modules.operational_graph.application.queries import validate_evidence
    from app.shared.errors import PermissionDeniedError

    graph_rows = await session.scalars(
        select(AgentEvidence).where(
            AgentEvidence.run_id == run_id, AgentEvidence.source_server == "business-graph"
        )
    )
    for source in graph_rows:
        await validate_evidence(session, user, source.content)
    rows = await session.scalars(
        select(AgentEvidence).where(
            AgentEvidence.run_id == run_id, AgentEvidence.source_server == "knowledge-rag"
        )
    )
    for version_id in {row.source_version for row in rows}:
        version, _, _ = await version_access(
            session, UUID(version_id), user, Permissions.KNOWLEDGE_READ
        )
        if version.status != "published":
            raise PermissionDeniedError(Permissions.KNOWLEDGE_READ)

    from datetime import UTC, datetime

    from app.modules.operation_event.domain.memory_models import CaseObservation

    observations = await session.scalars(
        select(AgentEvidence).where(
            AgentEvidence.run_id == run_id, AgentEvidence.source_server == "case-observation"
        )
    )
    for source in observations:
        note = await session.get(
            CaseObservation, UUID(source.source_resource), populate_existing=True
        )
        if note is None or note.status != "confirmed" or str(note.version) != source.source_version:
            raise PermissionDeniedError(Permissions.EVENT_READ)
    if check_memories:
        from app.modules.operation_event.application.memory import memory_access

        memories = await session.scalars(
            select(AgentEvidence).where(
                AgentEvidence.run_id == run_id, AgentEvidence.source_server == "experience-memory"
            )
        )
        for source in memories:
            row = await memory_access(session, user, UUID(source.source_resource))
            if (
                row.status != "approved"
                or row.expires_at <= datetime.now(UTC)
                or str(row.version) != source.source_version
            ):
                raise PermissionDeniedError(Permissions.MAINTENANCE_READ)
    # Stored snapshots never bypass current business source permissions.
    business = await session.scalars(
        select(AgentEvidence).where(
            AgentEvidence.run_id == run_id,
            AgentEvidence.source_server.in_(["event-tools", "work-order-tools", "handover-tools"]),
        )
    )
    from app.shared.security.authorization.dependencies import require_data_scope

    mapping = {
        "event-tools": (ProductionEvent, Permissions.EVENT_READ),
        "work-order-tools": (WorkOrder, Permissions.MAINTENANCE_READ),
        "handover-tools": (ManualOperationRecord, Permissions.HANDOVER_READ),
    }
    for source in business:
        model, permission = mapping[source.source_server]
        record = await session.get(model, UUID(source.source_resource), populate_existing=True)
        if record is None:
            raise PermissionDeniedError(permission)
        require_data_scope(
            user,
            record.organization_unit_id,
            permission,
            owner_id=record.created_by,
            assignee_id=record.assignee_id if isinstance(record, WorkOrder) else None,
        )
        actual = (
            str(record.version) if isinstance(record, WorkOrder) else str(record.current_version_id)
        )
        if actual != source.source_version:
            raise PermissionDeniedError(permission)
        if isinstance(record, ProductionEvent) and record.business_status != "confirmed":
            raise PermissionDeniedError(permission)

    from app.modules.operation_event.application.site_context import validate_saved_context

    contexts = list(
        await session.scalars(
            select(AgentEvidence).where(
                AgentEvidence.run_id == run_id, AgentEvidence.source_server == "site-context"
            )
        )
    )
    proposal = await session.scalar(select(AgentProposal).where(AgentProposal.run_id == run_id))
    cited = set(proposal.evidence_ids) if proposal else {str(row.id) for row in contexts}
    latest = current_evidence(contexts, include_expired=True)
    latest_ids = {str(row.id) for row in latest}
    if require_current_context and any(
        str(row.id) in cited and str(row.id) not in latest_ids for row in contexts
    ):
        raise ConflictError("proposal cites superseded external evidence")
    for source in latest:
        await validate_saved_context(
            session,
            user,
            source.content,
            require_fresh=require_current_context and str(source.id) in cited,
        )

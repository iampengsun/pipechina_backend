"""Business-specific multi-agent production-event investigation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.operation_event.application.budget import (
    check_usage,
    reserve,
    reserve_request,
    settle_request,
)
from app.modules.operation_event.application.context import current_evidence, role_context
from app.modules.operation_event.application.investigation_tools import InvestigationToolRegistry
from app.modules.operation_event.application.prompts import role_system_prompt
from app.modules.operation_event.application.skills import catalog, load_selected
from app.modules.operation_event.domain.agent_models import (
    AgentEvidence,
    AgentProposal,
    AgentQualityEvaluation,
    AgentRun,
    AgentStep,
    AgentTask,
)
from app.modules.operation_event.domain.agent_schemas import (
    CoordinatorPlan,
    CriticReport,
    EvidenceDecision,
    EvidenceReport,
    PlanReport,
    RiskReport,
)
from app.modules.operation_event.domain.memory_schemas import ExperienceApplicability
from app.modules.operation_event.domain.models import ProductionEvent, ProductionEventVersion
from app.ports.text import TextLLMProvider, take_text_usage
from app.shared.errors import ConflictError
from app.shared.platform.execution import guard_execution
from app.shared.platform.models import AsyncJob
from app.shared.platform.runtime import render_user_prompt
from app.shared.platform.service import add_ai_call_log


class InvestigationState(TypedDict, total=False):
    focus: list[str]
    evidence: dict[str, Any]
    risk: dict[str, Any]
    plan: dict[str, Any]
    critic: dict[str, Any]
    issues: list[str]
    experience_analysis: dict[str, Any]
    revision_count: int
    investigations: list[dict]
    input_questions: list[str]
    branches: list[dict]


async def _task(
    session: AsyncSession, run: AgentRun, role: str, goal: str, depends: list[str]
) -> AgentTask:
    """创建一个角色任务并刷新数据库，取得后续步骤使用的任务标识。"""
    item = AgentTask(run_id=run.id, role=role, goal=goal, depends_on=depends)
    session.add(item)  # 将当前记录加入当前事务
    await session.flush()  # 将待写对象发送到数据库以取得标识，不提交事务
    return item  # 返回新建角色任务


async def _call(
    session: AsyncSession,
    *,
    job: AsyncJob,
    run: AgentRun,
    task: AgentTask,
    provider: TextLLMProvider,
    operation: str,
    model: Any,
    payload: dict,
    system_prompt_override: str | None = None,
    user_prompt_override: str | None = None,
):
    """调用结构化模型，并在成功或异常后记录步骤、用量和调用日志。"""
    await reserve(session, run, job, UUID(str(job.execution_token)), "model")
    selected = run.config_snapshot.get("selected_skills", [])
    payload = {
        **payload,
        "skill_versions": [
            {k: v for k, v in skill.items() if k != "instructions"} for skill in selected
        ],
    }
    started = datetime.now(UTC)
    span_id = uuid4()
    status, error = "succeeded", None
    take_text_usage()  # 清空上下文中上一模型调用残留的用量
    system = role_system_prompt(operation, run.config_snapshot)
    system = system_prompt_override if system_prompt_override is not None else system
    user_prompt = (
        user_prompt_override
        if user_prompt_override is not None
        else render_user_prompt(
            run.config_snapshot.get("runtime", {}), json.dumps(payload, ensure_ascii=False)
        )
    )
    provider = await reserve_request(
        session,
        run,
        job,
        UUID(str(job.execution_token)),
        provider,
        system,
        user_prompt,
        model,
        span_id,
    )
    try:
        result = await provider.generate_structured(  # 调用模型并按指定输出模型校验返回结构
            operation=operation,
            system_prompt=system,
            user_prompt=user_prompt,
            response_model=model,
        )
        return result  # 返回通过结构校验的模型结果
    except BaseException as exc:
        status = "timeout" if isinstance(exc, TimeoutError) else "failed"
        error = type(exc).__name__
        raise
    finally:
        usage = take_text_usage()  # 取出本次调用用量，即使调用异常也记录
        await guard_execution(session, job.id, UUID(str(job.execution_token)))
        await session.refresh(run, with_for_update={"key_share": True})
        settle_request(
            run, span_id, usage_missing=usage.input_tokens is None or usage.output_tokens is None
        )
        input_tokens = usage.input_tokens or 0  # 缺失用量按零参与汇总，步骤仍保留空值
        output_tokens = usage.output_tokens or 0  # 缺失用量按零参与汇总，不表示模型免费
        pricing = run.config_snapshot.get(
            "pricing_microusd_per_million", {}
        )  # 读取每百万 Token 的微美元价格，当前入口默认零
        cost = (
            input_tokens * int(pricing.get("input", 0))
            + output_tokens * int(pricing.get("output", 0))
        ) // 1_000_000
        duration_ms = max(0, int((datetime.now(UTC) - started).total_seconds() * 1000))
        session.add(  # 将调用步骤加入当前事务
            AgentStep(
                task_id=task.id,  # 所属角色任务标识
                step_index=task.step_count,  # 任务内的步骤序号
                action="model_call",  # 步骤动作或取证决策
                tool_name=operation,  # 调用的工具或模型操作名称
                observation={"status": status},  # 本次调用的可审计结果
                trace_id=run.trace_id,  # 整次调查的链路标识
                span_id=span_id,  # 单次调用链路标识
                duration_ms=duration_ms,  # 调用耗时，单位为毫秒
                input_tokens=usage.input_tokens,  # 模型输入用量
                output_tokens=usage.output_tokens,  # 模型输出用量
                cost_microusd=cost,  # 按配置价格折算的微美元成本
            )
        )
        task.step_count += 1  # 将本次调用计入角色累计用量或步骤数
        task.input_tokens += input_tokens  # 将本次调用计入角色累计用量或步骤数
        task.output_tokens += output_tokens  # 将本次调用计入角色累计用量或步骤数
        task.cost_microusd += cost  # 将本次调用计入角色累计用量或步骤数
        run.input_tokens += input_tokens  # 将本次调用计入整次调查用量
        run.output_tokens += output_tokens  # 将本次调用计入整次调查用量
        run.cost_microusd += cost  # 将本次调用计入整次调查用量
        await add_ai_call_log(  # 记录供应商、模型、请求标识和调用用量
            session,
            job=job,
            run_id=run.id,  # 所属调查运行标识
            capability="text",
            provider=provider.name,
            model_alias=provider.model,
            model_name=provider.model,
            provider_request_id=usage.provider_request_id,
            started_at=started,  # 开始执行时间
            status=status,  # 当前执行状态
            error_code=error,
            input_units=usage.input_tokens,
            output_units=usage.output_tokens,
        )


async def _ensure_active(session: AsyncSession, run: AgentRun) -> None:
    """刷新调查运行状态，收到取消请求时标记取消并中断协作。"""
    await session.refresh(run)  # 重新读取调查运行，检查外部取消请求
    if run.cancel_requested:  # 识别调查运行层面的取消请求
        run.status = "cancelled"
        run.completed_at = datetime.now(UTC)
        raise ConflictError("multi-agent run cancelled")


async def run_multi_agent_assessment(
    session: AsyncSession,
    *,
    run: AgentRun,
    event: ProductionEvent,
    version: ProductionEventVersion,
    job: AsyncJob,
    provider: TextLLMProvider,
    tools: InvestigationToolRegistry,
    execution_token: UUID,
) -> AgentProposal | None:
    """Run a bounded investigation graph and save its human-review proposal."""
    run.status = "running"
    run.started_at = run.started_at or datetime.now(UTC)
    tasks = {
        task.role: task
        for task in await session.scalars(select(AgentTask).where(AgentTask.run_id == run.id))
    }
    known = list(await session.scalars(select(AgentEvidence).where(AgentEvidence.run_id == run.id)))
    evidence_task = tasks.get("evidence")
    previous_steps = (
        list(await session.scalars(select(AgentStep).where(AgentStep.task_id == evidence_task.id)))
        if evidence_task
        else []
    )
    seen = {
        (str(step.tool_name), str(step.tool_arguments.get("query", "")))
        for step in previous_steps
        if step.action == "tool_call"
        and step.tool_arguments.get("input_round", 0) == run.config_snapshot.get("input_round", 0)
    }
    step_budget = int(run.config_snapshot.get("evidence_step_budget", 6))
    evidence_decisions = sum(
        step.action == "model_call" and step.tool_name == "multi_agent_evidence"
        for step in previous_steps
    )

    async def finish_node() -> None:
        await guard_execution(session, job.id, execution_token)
        check_usage(run)
        await session.commit()

    async def activate(role: str, goal: str, depends: list[str]) -> AgentTask:
        await _ensure_active(session, run)
        task = tasks.get(role)
        if task is None:
            task = await _task(session, run, role, goal, depends)
            tasks[role] = task
        return task

    async def coordinate(state: InvestigationState) -> InvestigationState:
        task = await activate("coordinator", "制定调查计划", [])
        if task.status == "succeeded":
            return {
                "focus": task.output["focus"],
                "investigations": task.output.get("investigations", []),
            }
        task.status = "running"
        run.current_stage = "coordinator"
        task.input_snapshot = {"revision_count": 0}
        plan = await _call(
            session,
            job=job,
            run=run,
            task=task,
            provider=provider,
            operation="multi_agent_coordinator",
            model=CoordinatorPlan,
            payload={
                "case_memory": run.config_snapshot.get("case_context", {}),
                "event": event.title,
                "description": version.description,
                "tools": tools.list_tools(),
                "available_skills": [skill.summary() for skill in catalog().values()],
                "planning_contract": (
                    "按本次事件需要选择最多3个技能和独立子调查(kind/query/completion_criteria/required)。"
                    "明确完成条件；缺失将影响处置安全的子问题标记required。只选择可"
                    "用工具。复杂调查优先并行核查规范、历史与实测；缺关键事实时用questions请求人工补证。不要为凑流程创建无关分支。"
                ),
            },
        )
        if set(plan.roles) != {"evidence", "risk", "plan", "critic"}:
            raise ConflictError("coordinator returned an invalid role plan")
        names = run.config_snapshot.get("requested_skills")
        names = plan.skills if names is None else names
        run.config_snapshot = {
            **run.config_snapshot,
            "selected_skills": load_selected(names, run.config_snapshot.get("skill_catalog")),
        }
        task.output = plan.model_dump()
        task.status = "succeeded"
        await finish_node()
        return {
            "focus": plan.focus,
            "investigations": [row.model_dump() for row in plan.investigations],
        }

    async def investigate_branches(state):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.modules.operation_event.application.subinvestigations import execute_branches

        plans = state.get("investigations", [])
        if not plans:
            return {"branches": []}
        await finish_node()
        branches = await execute_branches(
            async_sessionmaker(session.bind, expire_on_commit=False, autoflush=False),
            run.id,
            plans,
            revision=state.get("revision_count", 0),
            provider=provider,
            knowledge=getattr(tools, "knowledge", None),
            site_context=getattr(tools, "site_context", None),
            token=execution_token,
        )
        await session.refresh(run)
        existing = {row.id for row in known}
        known.extend(
            row
            for row in await session.scalars(
                select(AgentEvidence).where(AgentEvidence.run_id == run.id)
            )
            if row.id not in existing
        )
        return {"branches": branches}

    async def gather(state: InvestigationState) -> InvestigationState:
        nonlocal evidence_decisions
        task = await activate("evidence", "收集可核验证据", ["coordinator"])
        if task.status == "succeeded" and task.input_snapshot.get("revision_count") == state.get(
            "revision_count", 0
        ):
            return {"evidence": task.output}
        task.status = "running"
        run.current_stage = "evidence"
        prior_hashes = {item.content_hash for item in known}
        task.input_snapshot = {
            "revision_count": state.get("revision_count", 0),
            "focus": state["focus"],
            "review_issues": state.get("issues", []),
        }
        if not any(
            e.source_server == "event-tools"
            and e.source_resource == str(event.id)
            and e.source_version == str(version.id)
            for e in known
        ):
            initial_content = {
                "event_id": str(event.id),
                "version_id": str(version.id),
                "text": version.description,
            }
            initial = AgentEvidence(
                run_id=run.id,
                task_id=task.id,
                source_server="event-tools",
                source_resource=str(event.id),
                source_version=str(version.id),
                content=initial_content,
                content_hash=hashlib.sha256(
                    json.dumps(initial_content, sort_keys=True).encode()
                ).hexdigest(),
                access_scope={"organization_unit_id": str(run.organization_unit_id)},
            )
            session.add(initial)
            await session.flush()
            known.append(initial)
        unknowns: list[str] = []
        questions = []
        # Current confirmed observations are primary facts, not inferred memories.
        from app.modules.operation_event.domain.memory_models import CaseObservation

        for note_data in run.config_snapshot.get("case_context", {}).get("observations", []):
            if any(
                e.source_server == "case-observation" and e.source_resource == note_data["id"]
                for e in known
            ):
                continue
            note = await session.get(CaseObservation, UUID(note_data["id"]), populate_existing=True)
            if note is None or note.status != "confirmed" or note.version != note_data["version"]:
                raise ConflictError("case observation changed during investigation")
            item = AgentEvidence(
                run_id=run.id,
                task_id=task.id,
                source_server="case-observation",
                source_resource=str(note.id),
                source_version=str(note.version),
                content={
                    "text": note.content,
                    "event_version_id": str(note.event_version_id),
                    "confirmed": True,
                },
                content_hash=hashlib.sha256(note.content.encode()).hexdigest(),
                access_scope={"organization_unit_id": str(run.organization_unit_id)},
            )
            session.add(item)
            await session.flush()
            known.append(item)
        while evidence_decisions < step_budget:
            await _ensure_active(session, run)
            decision = await _call(
                session,
                job=job,
                run=run,
                task=task,
                provider=provider,
                operation="multi_agent_evidence",
                model=EvidenceDecision,
                payload=role_context(
                    "multi_agent_evidence",
                    {
                        "event": version.description,
                        "focus": state["focus"],
                        "review_issues": state.get("issues", []),
                        "tools": tools.list_tools(),
                        "evidence": [item.content for item in known],
                        "case_memory": run.config_snapshot.get("case_context", {}),
                        "remaining_steps": step_budget - evidence_decisions,
                        "step_budget": step_budget,
                        "branch_results": state.get("branches", []),
                        "input_contract": (
                            "缺少影响处置的关键事实时finish并填写questions；普通未知不必暂停。"
                        ),
                    },
                    known,
                    limit=run.config_snapshot.get("limits", {}).get("context_chars", 16000),
                ),
            )
            evidence_decisions += 1
            if decision.action == "finish":
                unknowns = list(
                    dict.fromkeys(
                        [
                            *decision.unknowns,
                            *(
                                message
                                for branch in state.get("branches", [])
                                for message in branch.get("unknowns", [])
                            ),
                        ]
                    )
                )
                questions = list(
                    dict.fromkeys(
                        [
                            *decision.questions,
                            *(
                                q
                                for branch in state.get("branches", [])
                                for q in branch.get("questions", [])
                            ),
                        ]
                    )
                )
                break
            key = (decision.action, decision.query)
            if key in seen:
                raise ConflictError("evidence agent repeated a tool call")
            seen.add(key)
            tool_started = datetime.now(UTC)
            await reserve(session, run, job, execution_token, "tool")
            found = await tools.call(
                session,
                run=run,
                task=task,
                tool_name=decision.action,
                query=decision.query,
            )
            known.extend(found)
            session.add(
                AgentStep(
                    task_id=task.id,
                    step_index=task.step_count,
                    action="tool_call",
                    tool_name=decision.action,
                    tool_arguments={
                        "query": decision.query,
                        "input_round": run.config_snapshot.get("input_round", 0),
                    },
                    observation={"evidence_ids": [str(item.id) for item in found]},
                    trace_id=run.trace_id,
                    span_id=uuid4(),
                    duration_ms=max(
                        0, int((datetime.now(UTC) - tool_started).total_seconds() * 1000)
                    ),
                )
            )
            task.step_count += 1
        else:
            raise ConflictError("evidence agent exceeded step budget")
        if state.get("critic", {}).get("decision") == "insufficient_evidence" and not any(
            item.content_hash not in prior_hashes for item in known
        ):
            raise ConflictError("evidence agent found no new evidence")
        available = current_evidence(known)
        report = EvidenceReport(
            evidence_ids=[str(item.id) for item in available],
            facts=[
                str(item.content.get("text") or item.content.get("title") or "")
                for item in available
            ],
            unknowns=unknowns,
        )
        task.output = report.model_dump()
        task.status = "succeeded"
        await finish_node()
        return {"evidence": report.model_dump(), "input_questions": questions}

    async def analyze_experiences(state: InvestigationState) -> InvestigationState:
        ids = set(state["evidence"]["evidence_ids"])
        memories = [
            item
            for item in known
            if item.source_server == "experience-memory" and str(item.id) in ids
        ]
        if not memories:
            return {}
        task = await activate("experience", "比较历史经验的适用条件与本次事实差异", ["evidence"])
        if task.status == "succeeded" and task.input_snapshot.get("revision_count") == state.get(
            "revision_count", 0
        ):
            analysis = ExperienceApplicability.model_validate(task.output)
        else:
            task.status = "running"
            task.input_snapshot = {"revision_count": state.get("revision_count", 0)}
            analysis = await _call(
                session,
                job=job,
                run=run,
                task=task,
                provider=provider,
                operation="multi_agent_experience",
                model=ExperienceApplicability,
                payload={
                    "event": version.description,
                    "current_evidence": state["evidence"],
                    "experiences": [
                        {"evidence_id": str(item.id), "content": item.content} for item in memories
                    ],
                },
            )
            memory_ids = {str(item.id) for item in memories}
            applicable, excluded = (
                set(analysis.applicable_evidence_ids),
                set(analysis.excluded_evidence_ids),
            )
            if applicable & excluded or applicable | excluded != memory_ids:
                raise ConflictError(
                    "experience analyst must classify every retrieved experience exactly once"
                )
            task.output = analysis.model_dump()
            task.status = "succeeded"
            await finish_node()
        excluded = set(analysis.excluded_evidence_ids)
        filtered = [item for item in known if str(item.id) in ids and str(item.id) not in excluded]
        report = EvidenceReport(
            evidence_ids=[str(item.id) for item in filtered],
            facts=[
                str(item.content.get("text") or item.content.get("title") or "")
                for item in filtered
            ],
            unknowns=[*state["evidence"]["unknowns"], *analysis.unknowns],
        )
        return {"evidence": report.model_dump(), "experience_analysis": analysis.model_dump()}

    async def assess_risk(state: InvestigationState) -> InvestigationState:
        task = await activate("risk", "分类并评估风险", ["evidence"])
        if task.status == "succeeded" and task.input_snapshot.get("revision_count") == state.get(
            "revision_count", 0
        ):
            return {"risk": task.output}
        task.status = "running"
        run.current_stage = "risk"
        task.input_snapshot = {
            "revision_count": state.get("revision_count", 0),
            "evidence_ids": state["evidence"]["evidence_ids"],
        }
        risk = await _call(
            session,
            job=job,
            run=run,
            task=task,
            provider=provider,
            operation="multi_agent_risk",
            model=RiskReport,
            payload=role_context(
                "multi_agent_risk",
                {
                    "event": version.description,
                    "focus": state["focus"],
                    "experience_analysis": state.get("experience_analysis", {}),
                    "subinvestigations": state.get("branches", []),
                    "evidence": state["evidence"],
                },
                known,
                limit=run.config_snapshot.get("limits", {}).get("context_chars", 16000),
            ),
        )
        if risk.risk_level not in {"low", "medium", "high", "critical"}:
            raise ConflictError("risk agent returned an invalid risk level")
        if not set(risk.evidence_ids) <= set(state["evidence"]["evidence_ids"]):
            raise ConflictError("risk agent referenced unknown evidence")
        risk.unknowns = list(dict.fromkeys([*risk.unknowns, *state["evidence"]["unknowns"]]))
        task.output = risk.model_dump()
        task.status = "succeeded"
        await finish_node()
        return {"risk": risk.model_dump()}

    async def propose_plan(state: InvestigationState) -> InvestigationState:
        task = await activate("plan", "形成处置建议", ["risk"])
        if task.status == "succeeded" and task.input_snapshot.get("revision_count") == state.get(
            "revision_count", 0
        ):
            return {"plan": task.output}
        task.status = "running"
        run.current_stage = "plan"
        task.input_snapshot = {
            "revision_count": state.get("revision_count", 0),
            "evidence_ids": state["evidence"]["evidence_ids"],
            "review_issues": state.get("issues", []),
        }
        plan = await _call(
            session,
            job=job,
            run=run,
            task=task,
            provider=provider,
            operation="multi_agent_plan",
            model=PlanReport,
            payload=role_context(
                "multi_agent_plan",
                {
                    "focus": state["focus"],
                    "risk": state["risk"],
                    "evidence": state["evidence"],
                    "review_issues": state.get("issues", []),
                    "previous_plan": state.get("plan"),
                },
                known,
                limit=run.config_snapshot.get("limits", {}).get("context_chars", 16000),
            ),
        )
        if not set(plan.evidence_ids) <= set(state["evidence"]["evidence_ids"]):
            raise ConflictError("plan agent referenced unknown evidence")
        task.output = plan.model_dump()
        task.status = "succeeded"
        await finish_node()
        return {"plan": plan.model_dump()}

    async def review(state: InvestigationState) -> InvestigationState:
        task = await activate("critic", "复核证据和建议", ["risk", "plan"])
        if task.status == "succeeded" and task.input_snapshot.get("revision_count") == state.get(
            "revision_count", 0
        ):
            critic = CriticReport.model_validate(task.output)
        else:
            task.status = "running"
            run.current_stage = "critic"
            task.input_snapshot = {
                "evidence_ids": state["evidence"]["evidence_ids"],
                "revision_count": state.get("revision_count", 0),
            }
            evidence_sources = [
                {
                    "evidence_id": str(item.id),
                    "source": item.source_server,
                    "version": item.source_version,
                    "content": item.content,
                }
                for item in known
                if str(item.id) in set(state["evidence"]["evidence_ids"])
            ]
            from app.modules.operation_event.application.context import evidence_view

            evidence_sources = evidence_view(
                [item for item in known if str(item.id) in set(state["evidence"]["evidence_ids"])],
                limit=run.config_snapshot.get("limits", {}).get("context_chars", 16000),
            )
            critic = await _call(
                session,
                job=job,
                run=run,
                task=task,
                provider=provider,
                operation="multi_agent_critic",
                model=CriticReport,
                payload={
                    "focus": state["focus"],
                    "risk": state["risk"],
                    "plan": state["plan"],
                    "valid_evidence": state["evidence"]["evidence_ids"],
                    "evidence_sources": evidence_sources,
                    "confirmed_observations": run.config_snapshot.get("case_context", {}).get(
                        "observations", []
                    ),
                    "previous_issues": state.get("issues", []),
                },
            )
            if not set(critic.evidence_ids) <= set(state["evidence"]["evidence_ids"]):
                raise ConflictError("critic referenced unknown evidence")
            task.output = critic.model_dump()
            task.status = "succeeded"
            await finish_node()
        if critic.decision != "approved_for_human_review":
            if state.get("revision_count", 0) >= 2:
                if critic.decision == "insufficient_evidence":
                    return {
                        "critic": critic.model_dump(),
                        "input_questions": critic.issues or ["请补充当前事件的关键现场事实。"],
                    }
                raise ConflictError(
                    "multi-agent proposal requires more evidence", issues=critic.issues
                )
            return {
                "critic": critic.model_dump(),
                "issues": critic.issues,
                "revision_count": state.get("revision_count", 0) + 1,
            }
        return {"critic": critic.model_dump()}

    def route_review(state: InvestigationState) -> str:
        if state.get("input_questions"):
            return "human_input"
        decision = state["critic"]["decision"]
        if decision == "insufficient_evidence":
            return "evidence"
        if decision == "needs_revision":
            return "plan"
        return "approved"

    async def human_input(state):
        task = await activate("human_input", "等待人工确认补充事实", ["evidence"])
        if task.status != "waiting_input":
            cycle = int(task.input_snapshot.get("request_version", 0)) + 1
            if cycle > 3:
                raise ConflictError("human input cycle budget exhausted")
            task.status = "waiting_input"
            task.input_snapshot = {
                "request_version": cycle,
                "questions": state["input_questions"],
                "event_version_id": str(version.id),
                "observation_revision": run.config_snapshot.get("observation_revision"),
                "created_at": datetime.now(UTC).isoformat(),
            }
            task.output = {}
            await finish_node()
        response = interrupt({"request_id": str(task.id), **task.input_snapshot})
        if response != task.output or not task.output.get("confirmed_observation_ids"):
            raise ConflictError("human input does not match confirmed business records")
        task.status = "succeeded"
        await finish_node()
        return {
            "input_questions": [],
            "issues": [],
            "critic": {},
            "revision_count": state.get("revision_count", 0) + 1,
        }

    builder = StateGraph(InvestigationState)
    builder.add_node("coordinator", coordinate)
    builder.add_node("subinvestigations", investigate_branches)
    builder.add_node("human_input", human_input)
    builder.add_node("evidence", gather)
    builder.add_node("experience", analyze_experiences)
    builder.add_node("risk", assess_risk)
    builder.add_node("plan", propose_plan)
    builder.add_node("critic", review)
    builder.add_edge(START, "coordinator")
    builder.add_edge("coordinator", "subinvestigations")
    builder.add_edge("subinvestigations", "evidence")
    builder.add_conditional_edges(
        "evidence", lambda state: "human_input" if state.get("input_questions") else "experience"
    )
    builder.add_edge("human_input", "subinvestigations")
    builder.add_edge("experience", "risk")
    builder.add_edge("risk", "plan")
    builder.add_edge("plan", "critic")
    builder.add_conditional_edges(
        "critic",
        route_review,
        {"evidence": "evidence", "plan": "plan", "human_input": "human_input", "approved": END},
    )
    engine = session.get_bind()
    if not isinstance(engine, Engine):
        raise RuntimeError("investigation requires a database engine")
    checkpoint_url = (
        engine.url.set(drivername="postgresql")
        .update_query_dict({"options": "-csearch_path=agent_checkpoints"})
        .render_as_string(hide_password=False)
    )
    await session.execute(text("CREATE SCHEMA IF NOT EXISTS agent_checkpoints"))
    # Saver setup uses CREATE INDEX CONCURRENTLY and cannot wait on our open transaction.
    await guard_execution(session, job.id, execution_token)
    await session.commit()
    async with (
        asyncio.timeout(600),
        AsyncPostgresSaver.from_conn_string(
            checkpoint_url, serde=JsonPlusSerializer(allowed_msgpack_modules=[])
        ) as checkpointer,
    ):
        await checkpointer.setup()
        graph = builder.compile(checkpointer=checkpointer)
        config: RunnableConfig = {"configurable": {"thread_id": str(run.id)}}
        snapshot = await graph.aget_state(config)
        initial: InvestigationState | Command | None = (
            None if snapshot.values else {"revision_count": 0, "issues": []}
        )
        resume = run.result.get("resume_input")
        if snapshot.interrupts and resume:
            initial = Command(resume=resume)
        state = await graph.ainvoke(initial, config, durability="sync")
        if state.get("__interrupt__"):
            run.status = "waiting_input"
            run.current_stage = "human_input"
            task = tasks["human_input"]
            run.result = {
                **run.result,
                "input_request": {"request_id": str(task.id), **task.input_snapshot},
            }
            from app.shared.platform.service import update_job

            await guard_execution(session, job.id, execution_token)
            await update_job(
                session,
                job,
                status="waiting_input",
                progress=50,
                message="等待人工确认补充事实",
                result={"agent_run_id": str(run.id), "input_required": True},
            )
            await session.commit()
            return None
        await _ensure_active(session, run)
        risk = RiskReport.model_validate(state["risk"])
        plan = PlanReport.model_validate(state["plan"])
        critic = CriticReport.model_validate(state["critic"])
        valid_ids = set(state["evidence"]["evidence_ids"])
        proposal = AgentProposal(
            run_id=run.id,
            category=risk.category,
            risk_level=risk.risk_level,
            rationale=risk.rationale,
            recommended_action=plan.recommended_action,
            evidence_ids=critic.evidence_ids,
            unknowns=risk.unknowns,
            conflicts=critic.conflicts,
        )
        session.add(proposal)
        await session.flush()
        evidence_coverage = 100 if proposal.evidence_ids else 0
        citation_validity = 100 if set(proposal.evidence_ids) <= valid_ids else 0
        completeness = (
            100
            if all(
                [
                    proposal.category,
                    proposal.risk_level,
                    proposal.rationale,
                    proposal.recommended_action,
                ]
            )
            else 0
        )
        conflict_score = (
            100 if not proposal.conflicts else max(0, 100 - 20 * len(proposal.conflicts))
        )
        overall = round(
            (evidence_coverage + citation_validity + completeness + conflict_score + 100 + 100) / 6
        )
        session.add(
            AgentQualityEvaluation(
                run_id=run.id,
                overall_score=overall,
                evidence_coverage=evidence_coverage,
                citation_validity=citation_validity,
                completeness=completeness,
                conflict_score=conflict_score,
                critic_score=100,
                model_judge_score=100,
                details={
                    "unknown_count": len(proposal.unknowns),
                    "conflict_count": len(proposal.conflicts),
                    "evidence_count": len(proposal.evidence_ids),
                    "model_judge": "critic_agent",
                    "revision_count": state["revision_count"],
                },
            )
        )
        run.status = "succeeded"
        run.current_stage = "human_review"
        run.completed_at = datetime.now(UTC)
        run.result = {**run.result, "proposal_id": str(proposal.id)}
        return proposal


async def get_agent_run(session: AsyncSession, run_id) -> AgentRun | None:
    """按运行标识查询调查记录，不存在时返回空值。"""
    return await session.scalar(select(AgentRun).where(AgentRun.id == run_id))

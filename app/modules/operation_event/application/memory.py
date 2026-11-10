"""Cross-run case continuity and human-governed experience consolidation."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import or_, select, text

from app.modules.maintenance_order.domain.models import WorkOrder, WorkOrderTransition
from app.modules.operation_event.domain.agent_models import AgentEvidence
from app.modules.operation_event.domain.memory_models import (
    CaseMemory,
    CaseObservation,
    ExperienceMemory,
)
from app.modules.operation_event.domain.memory_schemas import ExperienceDraft, MemoryCritique
from app.modules.operation_event.domain.models import ProductionEvent
from app.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.platform.leased_jobs import leased_job
from app.shared.platform.models import WorkflowRun
from app.shared.platform.runtime import build_runtime_snapshot, text_provider_for_job
from app.shared.platform.service import add_audit, create_job, update_job
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions as P


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


async def event_access(session, user, event_id, permission=P.EVENT_READ, lock=False):
    event = await session.get(
        ProductionEvent, event_id, with_for_update=lock, populate_existing=True
    )
    if event is None:
        raise NotFoundError("production_event", event_id)
    require_data_scope(user, event.organization_unit_id, permission, owner_id=event.created_by)
    return event


async def confirmed_observations(session, event):
    return list(
        await session.scalars(
            select(CaseObservation)
            .where(
                CaseObservation.event_id == event.id,
                CaseObservation.event_version_id == event.current_version_id,
                CaseObservation.status == "confirmed",
            )
            .order_by(CaseObservation.id)
            .limit(101)
        )
    )


async def observation_revision(session, event):
    rows = await confirmed_observations(session, event)
    if len(rows) > 100:
        raise ConflictError("too many case observations; consolidate the formal event version")
    return digest([(str(row.id), row.version) for row in rows])


async def add_observation(session, user, event_id, payload, *, observation_id=None):
    event = await event_access(session, user, event_id, P.EVENT_EDIT, lock=True)
    if observation_id is not None:
        existing = await session.get(CaseObservation, observation_id, populate_existing=True)
        if existing is not None:
            if (
                existing.event_id != event.id
                or existing.created_by != user.user_id
                or existing.event_version_id != payload.expected_version_id
                or existing.content != payload.content
            ):
                raise ConflictError("request_id already belongs to a different observation")
            return existing
    if payload.expected_version_id != event.current_version_id:
        raise ConflictError("event version changed")
    note = CaseObservation(
        **({"id": observation_id} if observation_id is not None else {}),
        event_id=event.id,
        event_version_id=event.current_version_id,
        content=payload.content,
        created_by=user.user_id,
    )
    session.add(note)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="case.observation.create",
        resource_type="production_event",
        resource_id=event.id,
        after={"observation_id": str(note.id)},
    )
    await session.commit()
    return note


async def review_observation(session, user, event_id, note_id, payload):
    event = await event_access(session, user, event_id, P.EVENT_REVIEW, lock=True)
    note = await session.get(CaseObservation, note_id, with_for_update=True, populate_existing=True)
    if note is None or note.event_id != event.id:
        raise NotFoundError("case_observation", note_id)
    if note.version != payload.expected_version:
        raise ConflictError("observation version changed")
    if payload.action == "confirm":
        if note.status != "pending" or note.event_version_id != event.current_version_id:
            raise ConflictError("only current pending observations can be confirmed")
        note.status, note.confirmed_by = "confirmed", user.user_id
    else:
        if note.status == "retracted":
            raise ConflictError("observation already retracted")
        note.status = "retracted"
    note.version += 1
    note.reason = payload.reason
    await add_audit(
        session,
        user=user,
        action="case.observation." + payload.action,
        resource_type="production_event",
        resource_id=event.id,
        reason=payload.reason,
        after={"observation_id": str(note.id), "status": note.status},
    )
    await session.commit()
    return note


async def case_context(session, user, event, *, for_model=False):
    from app.modules.operation_event.application.investigation_tools import (
        authorize_knowledge_evidence,
    )

    rows = await session.scalars(
        select(CaseMemory)
        .where(CaseMemory.event_id == event.id)
        .order_by(CaseMemory.created_at.desc())
        .limit(3)
    )
    revision = await observation_revision(session, event)
    summaries = []
    for row in rows:
        if row.event_version_id != event.current_version_id or row.observation_revision != revision:
            summaries.append(
                {
                    "run_id": str(row.run_id),
                    "stale": True,
                    "reason": "event version changed; previous conclusions are not current facts",
                }
            )
            continue
        try:
            await authorize_knowledge_evidence(
                session, user, row.run_id, require_current_context=for_model
            )
        except PermissionDeniedError, ConflictError:
            continue
        summaries.append({"run_id": str(row.run_id), "stale": False, **row.summary})
    notes = await confirmed_observations(session, event)
    if for_model and (len(notes) > 30 or sum(len(n.content) for n in notes) > 24000):
        raise ConflictError(
            "case observations exceed context budget; consolidate a new formal event version"
        )
    return {
        "summaries": summaries,
        "observation_revision": await observation_revision(session, event),
        "observations": [{"id": str(n.id), "version": n.version, "text": n.content} for n in notes],
        "instruction": (
            "历史模型结论属于候选判断，不是已确认事实。"
            "仅当前版本已确认观察可作为事实；旧版本记忆不得覆盖新证据。"
        ),
    }


async def save_case_memory(session, run, proposal):
    from app.modules.operation_event.domain.agent_models import AgentStep, AgentTask

    steps = list(
        await session.scalars(
            select(AgentStep)
            .join(AgentTask, AgentTask.id == AgentStep.task_id)
            .where(AgentTask.run_id == run.id, AgentStep.action == "tool_call")
            .order_by(AgentStep.created_at)
        )
    )
    memory = CaseMemory(
        event_id=run.event_id,
        event_version_id=run.event_version_id,
        run_id=run.id,
        observation_revision=run.config_snapshot.get("observation_revision", ""),
        summary={
            "queries_tried": [
                {"tool": step.tool_name, "query": step.tool_arguments.get("query", "")}
                for step in steps[:10]
            ],
            "candidate_rationale": proposal.rationale[:2000],
            "candidate_plan": proposal.recommended_action[:2000],
            "open_questions": proposal.unknowns[:20],
            "evidence_ids": proposal.evidence_ids,
            "review_status": "pending_human_review",
        },
    )
    session.add(memory)
    await session.flush()
    return memory


async def order_sources(session, user, order_ids):
    """Read immutable closed outcomes; flatten ancestry so every source is reauthorized."""
    from app.modules.operation_event.application.investigation_tools import (
        authorize_knowledge_evidence,
    )

    sources = []
    dependencies: set[str] = set()
    seen: set[UUID] = set()
    queue = list(order_ids)
    while queue:
        order_id = UUID(str(queue.pop(0)))
        if order_id in seen:
            continue
        if len(seen) >= 50:
            raise ConflictError("experience provenance exceeds 50 work orders")
        seen.add(order_id)
        order = await session.get(WorkOrder, order_id, populate_existing=True)
        if order is None:
            raise NotFoundError("work_order", order_id)
        require_data_scope(
            user,
            order.organization_unit_id,
            P.MAINTENANCE_READ,
            owner_id=order.created_by,
            assignee_id=order.assignee_id,
        )
        if order.status != "closed":
            raise ConflictError("only closed work orders can become experience")
        event = await event_access(session, user, order.event_id)
        timeline = list(
            await session.scalars(
                select(WorkOrderTransition)
                .where(WorkOrderTransition.work_order_id == order.id)
                .order_by(WorkOrderTransition.version_after)
            )
        )
        evidence = []
        run_id = None
        if order.source_workflow_run_id:
            workflow = await session.get(WorkflowRun, order.source_workflow_run_id)
            run_id = workflow.state_snapshot.get("agent_run_id") if workflow else None
        if run_id:
            await authorize_knowledge_evidence(session, user, UUID(run_id), check_memories=False)
            records = await session.scalars(
                select(AgentEvidence).where(AgentEvidence.run_id == UUID(run_id))
            )
            for row in records:
                if row.source_server == "experience-memory":
                    dependencies.add(row.source_resource)
                    previous = await session.get(ExperienceMemory, UUID(row.source_resource))
                    if previous is None or previous.status in {"withdrawn", "rejected"}:
                        raise ConflictError("a source experience has been withdrawn")
                    dependencies.update(previous.dependency_ids)
                    queue.extend(item["id"] for item in previous.source_orders)
                evidence.append(
                    {"id": str(row.id), "source": row.source_server, "content": row.content}
                )
        from app.modules.maintenance_order.domain.models import (
            WorkOrderAcceptance,
            WorkOrderResolution,
        )

        acceptance = await session.scalar(
            select(WorkOrderAcceptance)
            .where(
                WorkOrderAcceptance.work_order_id == order.id,
                WorkOrderAcceptance.approved.is_(True),
            )
            .order_by(WorkOrderAcceptance.created_at.desc())
            .limit(1)
        )
        resolution = (
            await session.get(WorkOrderResolution, acceptance.resolution_id) if acceptance else None
        )
        sources.append(
            {
                "id": str(order.id),
                "version": order.version,
                "organization_unit_id": str(order.organization_unit_id),
                "event_id": str(event.id),
                "title": order.title,
                "description": order.description,
                "risk_level": order.risk_level,
                "measured_outcome": {
                    "resolution_id": str(resolution.id),
                    "action": resolution.action,
                    "result": resolution.result,
                    "measurements": resolution.measurements,
                    "acceptance_id": str(acceptance.id),
                    "verification": acceptance.measurements,
                    "independently_accepted_by": str(acceptance.reviewed_by),
                }
                if resolution and acceptance
                else None,
                "timeline": [
                    {"id": str(t.id), "to_status": t.to_status, "reason": t.reason}
                    for t in timeline
                ],
                "evidence": evidence,
            }
        )
    if len({s["organization_unit_id"] for s in sources}) != 1:
        raise ConflictError("experience sources must belong to one organization")
    return sources, sorted(dependencies)


def allowed_evidence(sources):
    values = set()
    for source in sources:
        values.add(source["id"])
        values.update(t["id"] for t in source["timeline"])
        values.update(e["id"] for e in source["evidence"])
        outcome = source.get("measured_outcome")
        if outcome:
            values.update([outcome["resolution_id"], outcome["acceptance_id"]])
    return values


def fingerprints(content):
    body = {key: value for key, value in content.items() if key not in {"title", "evidence_ids"}}
    normalized = {
        key: sorted(value) if key in {"equipment_models", "preconditions", "cautions"} else value
        for key, value in body.items()
    }
    scope = {key: normalized[key] for key in ["equipment_models", "symptom", "preconditions"]}
    return digest(normalized), digest(scope)


async def check_candidates(session, org_id, content, excluded=()):
    hash_value, key = fingerprints(content)
    rows = list(
        await session.scalars(
            select(ExperienceMemory).where(
                ExperienceMemory.organization_unit_id == org_id,
                ExperienceMemory.status == "approved",
                ExperienceMemory.expires_at > datetime.now(UTC),
                or_(
                    ExperienceMemory.content_hash == hash_value,
                    ExperienceMemory.applicability_key == key,
                ),
            )
        )
    )
    return {
        "duplicate_ids": [
            str(r.id) for r in rows if r.id not in excluded and r.content_hash == hash_value
        ],
        "conflict_ids": [
            str(r.id) for r in rows if r.id not in excluded and r.content_hash != hash_value
        ],
    }


async def memory_access(session, user, memory_id, *, lock=False, check_sources=True):
    row = await session.get(
        ExperienceMemory, memory_id, with_for_update=lock, populate_existing=True
    )
    if row is None:
        raise NotFoundError("experience_memory", memory_id)
    require_data_scope(user, row.organization_unit_id, P.MAINTENANCE_READ, owner_id=row.created_by)
    if check_sources:
        sources, _ = await order_sources(session, user, [s["id"] for s in row.source_orders])
        if {s["id"]: s["version"] for s in sources} != {
            s["id"]: s["version"] for s in row.source_orders
        }:
            raise ConflictError("experience source version changed")
        for key in row.dependency_ids:
            dependency = await session.get(ExperienceMemory, UUID(key))
            if dependency is None or dependency.status in {"withdrawn", "rejected"}:
                raise ConflictError("experience dependency was withdrawn")
    return row


async def start_consolidation(
    session,
    user,
    order_ids,
    provider,
    *,
    expected_versions,
    expires_at=None,
    supersedes=(),
    merge_reason=None,
    dependencies=(),
    expected_memory_versions=None,
    inline=False,
):
    sources, source_dependencies = await order_sources(session, user, order_ids)
    dependencies = sorted(set(dependencies) | set(source_dependencies))
    for source in sources:
        if (
            source["id"] in expected_versions
            and source["version"] != expected_versions[source["id"]]
        ):
            raise ConflictError("work order version changed")
    org_id = UUID(sources[0]["organization_unit_id"])
    require_data_scope(user, org_id, P.EVENT_CLASSIFY, owner_id=user.user_id)
    expiry = expires_at or datetime.now(UTC) + timedelta(days=180)
    if expiry <= datetime.now(UTC):
        raise ConflictError("memory expiry must be in the future")
    if merge_reason is None:
        existing = await session.scalar(
            select(ExperienceMemory)
            .where(
                ExperienceMemory.primary_order_id == UUID(str(order_ids[0])),
                ExperienceMemory.parent_id.is_(None),
            )
            .limit(1)
        )
        if existing:
            return {
                "id": str(existing.id),
                "job_id": str(existing.job_id) if existing.job_id else None,
            }
    resource_id = (
        uuid5(NAMESPACE_URL, digest(expected_memory_versions or dependencies))
        if merge_reason is not None
        else UUID(str(order_ids[0]))
    )
    job = await create_job(
        session,
        user=user,
        job_type="experience_consolidation",
        resource_type="experience_merge" if merge_reason is not None else "work_order",
        resource_id=resource_id,
        task_name="app.modules.operation_event.consolidate_memory",
        queue="ai_text",
        config_snapshot=await build_runtime_snapshot(
            session,
            job_type="experience_consolidation",
            provider=provider,
            base={
                "sources": [{"id": s["id"], "version": s["version"]} for s in sources],
                "supersedes_ids": [str(i) for i in supersedes],
                "expires_at": expiry.isoformat(),
                "dependency_ids": dependencies,
                "merge_reason": merge_reason,
                "merge_sources": expected_memory_versions or {},
            },
        ),
        organization_unit_id=org_id,
        enqueue=not inline,
    )
    await session.commit()
    if inline:
        await execute_consolidation(session, job=job, user=user, provider=provider)
    return {"job_id": str(job.id)}


@leased_job
async def execute_consolidation(session, *, job, user, provider):
    if job.status in {"succeeded", "failed", "cancelled"}:
        return
    await update_job(session, job, status="running", progress=10)
    await session.commit()
    provider = text_provider_for_job(session, job, provider)
    sources, dependencies = await order_sources(
        session, user, [s["id"] for s in job.config_snapshot["sources"]]
    )
    if {s["id"]: s["version"] for s in sources} != {
        s["id"]: s["version"] for s in job.config_snapshot["sources"]
    }:
        raise ConflictError("memory source version changed")
    if len(json.dumps(sources, ensure_ascii=False)) > 120000:
        raise ConflictError("memory sources exceed context budget; consolidate fewer work orders")
    draft = await provider.generate_structured(
        operation="memory_consolidation",
        system_prompt="你是处置复盘智能体。只从已关闭工单和原始证据提炼可复用经验草稿；明确设备、适用前提、措施、实际结果与禁忌。不得把模型建议当作已执行动作，不得虚构结果。引用输入中真实的证据或工单ID。所有内容仅为待人工审核候选。",
        user_prompt=json.dumps({"sources": sources}, ensure_ascii=False),
        response_model=ExperienceDraft,
    )
    if not set(draft.evidence_ids) <= allowed_evidence(sources):
        raise ConflictError("memory draft referenced unknown evidence")
    critic = await provider.generate_structured(
        operation="memory_critic",
        system_prompt="你是经验记忆复核智能体。检查草稿是否由已关闭工单的执行记录和证据支持，是否把推测当事实、是否缺少适用条件。不能替代人工验收。引用必须来自输入。",
        user_prompt=json.dumps(
            {"sources": sources, "draft": draft.model_dump()}, ensure_ascii=False
        ),
        response_model=MemoryCritique,
    )
    if not set(critic.evidence_ids) <= allowed_evidence(sources):
        raise ConflictError("memory critic referenced unknown evidence")
    dependencies = sorted(set(dependencies) | set(job.config_snapshot["dependency_ids"]))
    from app.shared.worker_runtime import job_user

    user = await job_user(session, job)
    await order_sources(session, user, [s["id"] for s in job.config_snapshot["sources"]])
    for memory_id, expected in job.config_snapshot.get("merge_sources", {}).items():
        source_memory = await memory_access(session, user, UUID(memory_id))
        if source_memory.version != expected or source_memory.status not in {"pending", "approved"}:
            raise ConflictError("merge source changed during consolidation")
    body = draft.model_dump(mode="json")
    hash_value, key = fingerprints(body)
    checks = await check_candidates(session, job.organization_unit_id, body)
    memory = ExperienceMemory(
        organization_unit_id=job.organization_unit_id,
        primary_order_id=UUID(sources[0]["id"]),
        job_id=job.id,
        status="pending",
        version=1,
        content=body,
        content_hash=hash_value,
        applicability_key=key,
        search_text="\n".join(
            [
                draft.title,
                draft.symptom,
                *draft.equipment_models,
                *draft.preconditions,
                *draft.actions,
                draft.outcome,
            ]
        ),
        source_orders=[{"id": s["id"], "version": s["version"]} for s in sources],
        dependency_ids=dependencies,
        supersedes_ids=job.config_snapshot["supersedes_ids"],
        checks={**checks, "critic": critic.model_dump()},
        expires_at=datetime.fromisoformat(job.config_snapshot["expires_at"]),
        created_by=user.user_id,
    )
    session.add(memory)
    await session.flush()
    await update_job(
        session, job, status="succeeded", progress=100, result={"experience_id": str(memory.id)}
    )
    await session.commit()


async def lock_memory_space(session, org_id):
    # Serialize review/withdraw across one organization so conflicting approvals cannot race.
    value = int(hashlib.sha256(str(org_id).encode()).hexdigest()[:16], 16)
    if value >= 2**63:
        value -= 2**64
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": value})


async def review_memory(session, user, memory_id, payload):
    row = await memory_access(session, user, memory_id)
    await lock_memory_space(session, row.organization_unit_id)
    row = await memory_access(session, user, memory_id, lock=True)
    require_data_scope(
        user, row.organization_unit_id, P.MAINTENANCE_REVIEW, owner_id=row.created_by
    )
    if row.status != "pending" or row.version != payload.expected_version:
        raise ConflictError("memory is not the current pending revision")
    if row.created_by == user.user_id:
        raise ConflictError("experience requires an independent reviewer")
    if payload.approved:
        if row.expires_at <= datetime.now(UTC):
            raise ConflictError("experience has expired")
        if not row.checks.get("critic", {}).get("supported"):
            raise ConflictError("critic issues require a corrected revision before approval")
        checks = await check_candidates(session, row.organization_unit_id, row.content, [row.id])
        supersedes = set(row.supersedes_ids) | {str(i) for i in payload.supersedes_ids}
        if not set(checks["duplicate_ids"] + checks["conflict_ids"]) <= supersedes:
            raise ConflictError("explicitly resolve duplicate or conflicting memories", **checks)
        for old_id in sorted(supersedes):
            old = await memory_access(session, user, UUID(old_id), lock=True)
            if old.organization_unit_id != row.organization_unit_id or old.status != "approved":
                raise ConflictError("only approved same-organization memories can be superseded")
            old.status = "superseded"
            old.version += 1
        row.supersedes_ids = sorted(supersedes)
        row.status = "approved"
    else:
        row.status = "rejected"
    row.reviewed_by = user.user_id
    row.review_reason = payload.reason
    row.version += 1
    await add_audit(
        session,
        user=user,
        action="experience." + row.status,
        resource_type="experience_memory",
        resource_id=row.id,
        reason=payload.reason,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def revise_memory(session, user, memory_id, payload):
    original = await memory_access(session, user, memory_id, lock=True)
    require_data_scope(
        user, original.organization_unit_id, P.EVENT_CLASSIFY, owner_id=original.created_by
    )
    if original.version != payload.expected_version or original.status in {
        "withdrawn",
        "superseded",
    }:
        raise ConflictError("experience revision changed or is unavailable")
    if payload.expires_at <= datetime.now(UTC):
        raise ConflictError("expiry must be in the future")
    sources, _ = await order_sources(session, user, [s["id"] for s in original.source_orders])
    if not set(payload.content.evidence_ids) <= allowed_evidence(sources):
        raise ConflictError("revision referenced unknown source evidence")
    existing = await session.scalar(
        select(ExperienceMemory.id)
        .where(ExperienceMemory.parent_id == original.id, ExperienceMemory.status == "pending")
        .limit(1)
    )
    if existing:
        raise ConflictError("a pending revision already exists")
    content = payload.content.model_dump(mode="json")
    hash_value, key = fingerprints(content)
    row = ExperienceMemory(
        organization_unit_id=original.organization_unit_id,
        primary_order_id=original.primary_order_id,
        parent_id=original.id,
        status="pending",
        version=1,
        content=content,
        content_hash=hash_value,
        applicability_key=key,
        search_text=json.dumps(content, ensure_ascii=False),
        source_orders=original.source_orders,
        dependency_ids=original.dependency_ids,
        supersedes_ids=[str(original.id)]
        if original.status == "approved"
        else original.supersedes_ids,
        checks={
            **await check_candidates(session, original.organization_unit_id, content),
            "critic": {"supported": True, "method": "human_revision", "issues": []},
            "revision_reason": payload.reason,
        },
        expires_at=payload.expires_at,
        created_by=user.user_id,
    )
    if original.status in {"pending", "rejected"}:
        original.status = "superseded"
        original.version += 1
    session.add(row)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="experience.revise",
        resource_type="experience_memory",
        resource_id=row.id,
        reason=payload.reason,
    )
    await session.commit()
    return row


async def withdraw_memory(session, user, memory_id, payload):
    row = await memory_access(session, user, memory_id, check_sources=False)
    await lock_memory_space(session, row.organization_unit_id)
    row = await memory_access(session, user, memory_id, lock=True, check_sources=False)
    require_data_scope(
        user, row.organization_unit_id, P.MAINTENANCE_REVIEW, owner_id=row.created_by
    )
    if row.version != payload.expected_version or row.status in {"withdrawn", "rejected"}:
        raise ConflictError("experience revision changed or is already unavailable")
    rows = list(
        await session.scalars(
            select(ExperienceMemory)
            .where(
                ExperienceMemory.organization_unit_id == row.organization_unit_id,
                ExperienceMemory.status.in_(["approved", "pending"]),
            )
            .with_for_update()
        )
    )
    for candidate in rows:
        if candidate.id == row.id or str(row.id) in candidate.dependency_ids:
            candidate.status = "withdrawn"
            candidate.version += 1
            candidate.review_reason = payload.reason
    if row.status != "withdrawn":
        row.status = "withdrawn"
        row.version += 1
    await add_audit(
        session,
        user=user,
        action="experience.withdraw",
        resource_type="experience_memory",
        resource_id=row.id,
        reason=payload.reason,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def search_experiences(session, user, event, query):
    rows = list(
        await session.scalars(
            select(ExperienceMemory)
            .where(
                ExperienceMemory.organization_unit_id == event.organization_unit_id,
                ExperienceMemory.status == "approved",
                ExperienceMemory.expires_at > datetime.now(UTC),
                ExperienceMemory.search_text.contains(query[:100], autoescape=True),
            )
            .order_by(ExperienceMemory.updated_at.desc())
            .limit(10)
        )
    )
    result = []
    for row in rows:
        try:
            await memory_access(session, user, row.id)
        except PermissionDeniedError, ConflictError:
            continue
        result.append(
            {
                "id": str(row.id),
                "record_id": str(row.id),
                "version": row.version,
                "text": row.search_text,
                "experience": row.content,
                "sources": row.source_orders,
                "expires_at": row.expires_at.isoformat(),
                "instruction": "历史经验仅在适用条件满足时可参考，不替代当前规范和现场证据",
            }
        )
    return result[:5]

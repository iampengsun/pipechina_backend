"""Human input resumes the existing run and job after confirmed source validation."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.modules.operation_event.application.memory import case_context, event_access
from app.modules.operation_event.domain.agent_models import AgentRun, AgentTask
from app.modules.operation_event.domain.memory_models import CaseObservation
from app.shared.errors import ConflictError, NotFoundError
from app.shared.platform.models import AsyncJob, TaskOutbox
from app.shared.platform.service import add_audit, update_job
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.worker_runtime import job_user


async def run_access(session, user, run_id, *, lock=False):
    row = await session.get(AgentRun, run_id, populate_existing=True)
    if row is None:
        raise NotFoundError("agent_run", run_id)
    require_data_scope(user, row.organization_unit_id, P.EVENT_CLASSIFY, owner_id=row.created_by)
    if lock:
        await session.get(AsyncJob, row.job_id, with_for_update=True, populate_existing=True)
        await session.refresh(row, with_for_update=True)
    return row


async def read_request(session, user, run_id):
    run = await run_access(session, user, run_id)
    task = await session.scalar(
        select(AgentTask).where(AgentTask.run_id == run.id, AgentTask.role == "human_input")
    )
    return {
        "run_id": str(run.id),
        "status": run.status,
        "request": task.input_snapshot if task else None,
        "answer": task.output if task else None,
    }


async def resume(
    session,
    user,
    run_id,
    payload,
    *,
    inline=False,
    provider=None,
    knowledge=None,
    site_context=None,
):
    run = await run_access(session, user, run_id, lock=True)
    answer = {
        "request_version": payload.request_version,
        "confirmed_observation_ids": [str(i) for i in payload.confirmed_observation_ids],
    }
    if (
        run.status in {"queued", "running", "succeeded"}
        and run.result.get("resume_input") == answer
    ):
        return {"agent_run_id": str(run.id), "job_id": str(run.job_id), "status": run.status}
    if run.status != "waiting_input" or run.cancel_requested:
        raise ConflictError("investigation is not awaiting input")
    task = await session.scalar(
        select(AgentTask)
        .where(AgentTask.run_id == run.id, AgentTask.role == "human_input")
        .with_for_update()
    )
    if task is None or task.input_snapshot["request_version"] != payload.request_version:
        raise ConflictError("input request changed")
    event = await event_access(session, user, run.event_id, lock=True)
    if event.business_status != "confirmed" or event.current_version_id != run.event_version_id:
        raise ConflictError("event version changed; start a new investigation")
    if len(set(payload.confirmed_observation_ids)) != len(payload.confirmed_observation_ids):
        raise ConflictError("duplicate input observations")
    for identifier in payload.confirmed_observation_ids:
        note = await session.get(CaseObservation, identifier, populate_existing=True)
        if (
            note is None
            or note.event_id != event.id
            or note.event_version_id != run.event_version_id
            or note.status != "confirmed"
            or note.created_at < datetime.fromisoformat(task.input_snapshot["created_at"])
        ):
            raise ConflictError(
                "input requires newly recorded, human-confirmed observations for this event version"
            )
    job = await session.get(AsyncJob, run.job_id)
    original_user = await job_user(session, job)
    context = await case_context(session, original_user, event, for_model=True)
    run.config_snapshot = {
        **run.config_snapshot,
        "case_context": context,
        "input_round": payload.request_version,
        "observation_revision": context["observation_revision"],
        "evidence_step_budget": min(24, run.config_snapshot.get("evidence_step_budget", 6) + 6),
    }
    run.result = {**run.result, "resume_input": answer}
    run.status = "queued"
    task.output = answer
    for row in await session.scalars(
        select(AgentTask).where(
            AgentTask.run_id == run.id,
            AgentTask.role.in_(["evidence", "experience", "risk", "plan", "critic"]),
        )
    ):
        row.status = "pending"
        row.output = {}
    await update_job(
        session, job, status="queued", progress=50, message="人工补证已确认，等待继续调查"
    )
    if not inline:
        session.add(
            TaskOutbox(
                job_id=job.id,
                operation_key=f"resume:{run.id}:{payload.request_version}",
                task_name=job.config_snapshot["task_name"],
                queue=job.config_snapshot["queue"],
                payload={"args": [str(job.id)], "kwargs": {}},
                status="pending",
                available_at=datetime.now(UTC),
            )
        )
    await add_audit(
        session,
        user=user,
        action="agent.input.resume",
        resource_type="agent_run",
        resource_id=run.id,
        reason=payload.reason,
        after=answer,
    )
    await session.commit()
    if inline:
        from app.modules.operation_event.application.service import execute_event_classification
        from app.modules.operation_event.domain.models import ProductionEventVersion

        version = await session.get(ProductionEventVersion, run.event_version_id)
        await execute_event_classification(
            session,
            job=job,
            event=event,
            version=version,
            user=original_user,
            provider=provider,
            knowledge=knowledge,
            site_context=site_context,
        )
    return {"agent_run_id": str(run.id), "job_id": str(job.id), "status": run.status}

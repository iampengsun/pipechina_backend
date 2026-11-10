from app.modules.knowledge.application.retry import bind_retry
from app.modules.operation_event.application.optimization import bind_retry as bind_prompt_retry
from app.modules.operation_event.application.retry import bind_classification_retry


def retry_handlers():
    """返回任务类型到业务重试绑定器的映射。"""

    return {
        "knowledge_index": bind_retry,
        "knowledge_answer": bind_retry,
        "knowledge_evaluate": bind_retry,
        "event_classification": bind_classification_retry,
        "prompt_optimization": bind_prompt_retry,
    }


async def validate_prompt_activation(session, template_id):
    from app.modules.operation_event.application.optimization import validate_activation

    await validate_activation(session, template_id)


async def synchronize_cancelled_job(session, job):
    from sqlalchemy import select

    from app.modules.operation_event.domain.agent_models import AgentRun, PromptOptimization

    if job.job_type == "event_classification":
        row = await session.scalar(
            select(AgentRun).where(AgentRun.job_id == job.id).with_for_update()
        )
        if row:
            row.cancel_requested = job.cancel_requested
            if job.status in {"cancelled", "failed", "succeeded"}:
                row.status = job.status
                row.completed_at = job.completed_at
    elif job.job_type == "prompt_optimization":
        row = await session.scalar(
            select(PromptOptimization).where(PromptOptimization.job_id == job.id).with_for_update()
        )
        if row and job.status in {"cancelled", "failed", "succeeded"}:
            row.status = job.status

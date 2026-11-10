"""Start knowledge jobs for HTTP callers and cross-module publication workflows."""

from typing import Literal

from app.modules.knowledge.application.answers import execute_answer
from app.modules.knowledge.application.configuration import knowledge_snapshot
from app.modules.knowledge.application.evaluation import execute_evaluation
from app.modules.knowledge.application.pipeline import execute_ingestion
from app.modules.knowledge.domain.models import KnowledgeEvaluation, KnowledgeRun, KnowledgeVersion
from app.shared.platform.service import create_job


async def start_knowledge_job(
    session,
    user,
    resource: KnowledgeVersion | KnowledgeRun | KnowledgeEvaluation,
    kind: Literal["index", "answer", "evaluate"],
    *,
    settings,
    providers,
    knowledge_manager,
    session_factory,
    organization_unit_id=None,
):
    task = {"index": "index_document", "answer": "answer", "evaluate": "evaluate"}[kind]
    resource_type = {"index": "version", "answer": "run", "evaluate": "evaluation"}[kind]
    snapshot = dict(resource.snapshot) if isinstance(resource, KnowledgeVersion) else {}
    if not snapshot:
        snapshot = await knowledge_snapshot(session, settings)
    provider = providers.text
    snapshot.update(
        {
            "provider": snapshot.get("text_provider", provider.name),
            "model": snapshot.get("text_model", provider.model),
            "retry_handler": "knowledge",
        }
    )
    job = await create_job(
        session,
        user=user,
        job_type=f"knowledge_{kind}",
        resource_type=f"knowledge_{resource_type}",
        resource_id=resource.id,
        task_name=f"app.modules.knowledge.{task}",
        queue={
            "index": "knowledge_index",
            "answer": "knowledge_query",
            "evaluate": "knowledge_eval",
        }[kind],
        config_snapshot=snapshot,
        organization_unit_id=organization_unit_id,
        enqueue=not settings.run_tasks_inline,
    )
    if isinstance(resource, (KnowledgeRun, KnowledgeEvaluation)):
        resource.job_id = job.id
    await session.commit()
    if settings.run_tasks_inline:
        async with knowledge_manager.scoped(job.config_snapshot) as knowledge:
            common = dict(
                factory=session_factory,
                knowledge=knowledge,
                text=providers.text,
                settings=settings,
                job_id=job.id,
            )
            if kind == "index":
                await execute_ingestion(**common, storage=providers.storage)
            elif kind == "answer":
                await execute_answer(**common)
            else:
                await execute_evaluation(**common)
    return {"id": resource.id, "job_id": job.id}

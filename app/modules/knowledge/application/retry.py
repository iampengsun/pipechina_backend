"""Keep business runs and execution attempts in sync in one transaction."""

from app.modules.knowledge.application.access import version_access
from app.modules.knowledge.application.answers import run_access
from app.modules.knowledge.domain.models import KnowledgeEvaluation
from app.shared.errors import ConflictError, PermissionDeniedError
from app.shared.security.authorization.permissions import Permissions


async def bind_retry(session, original, retried, user):
    if original.job_type == "knowledge_index":
        await version_access(session, original.resource_id, user, Permissions.KNOWLEDGE_INDEX)
        return
    if original.job_type == "knowledge_answer":
        resource = await run_access(session, user, original.resource_id)
    else:
        if not user.has_permission(Permissions.KNOWLEDGE_EVALUATE):
            raise PermissionDeniedError(Permissions.KNOWLEDGE_EVALUATE)
        resource = await session.get(KnowledgeEvaluation, original.resource_id)
        if resource is None or resource.requested_by != user.user_id:
            raise PermissionDeniedError(Permissions.KNOWLEDGE_EVALUATE)
    await session.refresh(resource, with_for_update=True)
    if resource.job_id != original.id:
        raise ConflictError("only the current business execution can be retried")
    resource.job_id = retried.id
    resource.status = "queued"

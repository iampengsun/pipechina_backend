"""Knowledge lifecycle and grounded question answering API."""

import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select

from app.modules.knowledge.application import service
from app.modules.knowledge.application.access import base_access, document_access, version_access
from app.modules.knowledge.application.answers import run_access
from app.modules.knowledge.application.configuration import knowledge_snapshot
from app.modules.knowledge.application.evaluation import review_evaluation
from app.modules.knowledge.application.jobs import start_knowledge_job
from app.modules.knowledge.application.retrieval import retrieve
from app.modules.knowledge.domain.models import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeConversation,
    KnowledgeDocument,
    KnowledgeEvaluation,
    KnowledgeFeedback,
    KnowledgeRun,
    KnowledgeVersion,
)
from app.modules.knowledge.domain.schemas import (
    ACLUpdate,
    ArtifactCorrection,
    BaseCreate,
    ConversationCreate,
    DocumentCreate,
    EvaluationCreate,
    EvaluationExpertReview,
    FeedbackInput,
    Reason,
    Review,
    SearchInput,
    VersionCreate,
)
from app.shared.db import SessionDep
from app.shared.errors import AppError, ConflictError, NotFoundError, PermissionDeniedError
from app.shared.platform.models import AsyncJob
from app.shared.platform.service import add_audit, request_cancel, update_job
from app.shared.security.authentication.dependencies import _verify_token
from app.shared.security.authorization.dependencies import require_permission
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.authorization.schemas import CurrentUser
from app.shared.worker_runtime import job_user

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
Reader = Annotated[CurrentUser, Depends(require_permission(P.KNOWLEDGE_READ))]
Editor = Annotated[CurrentUser, Depends(require_permission(P.KNOWLEDGE_EDIT))]
Uploader = Annotated[CurrentUser, Depends(require_permission(P.KNOWLEDGE_UPLOAD))]
Indexer = Annotated[CurrentUser, Depends(require_permission(P.KNOWLEDGE_INDEX))]
Reviewer = Annotated[CurrentUser, Depends(require_permission(P.KNOWLEDGE_REVIEW))]
Publisher = Annotated[CurrentUser, Depends(require_permission(P.KNOWLEDGE_PUBLISH))]
Withdrawer = Annotated[CurrentUser, Depends(require_permission(P.KNOWLEDGE_WITHDRAW))]
Evaluator = Annotated[CurrentUser, Depends(require_permission(P.KNOWLEDGE_EVALUATE))]


def version_view(version):
    return {
        key: getattr(version, key)
        for key in (
            "id",
            "document_id",
            "number",
            "status",
            "filename",
            "mime_type",
            "size_bytes",
            "sha256",
            "effective_from",
            "effective_to",
            "equipment_models",
            "authority",
            "generation",
            "snapshot",
            "index_state",
            "review_reason",
        )
    }


@router.post("/bases", status_code=201)
async def create_base(payload: BaseCreate, session: SessionDep, user: Editor):
    base = await service.create_base(session, user, payload)
    return {"id": base.id, "name": base.name, "acl_version": base.acl_version}


@router.post("/documents", status_code=201)
async def create_document(payload: DocumentCreate, session: SessionDep, user: Editor):
    document = await service.create_document(session, user, payload)
    return {"id": document.id, "code": document.code, "title": document.title}


@router.get("/bases/{base_id}/documents")
async def documents(base_id: UUID, session: SessionDep, user: Reader):
    await base_access(session, base_id, user, P.KNOWLEDGE_READ)
    rows = await session.scalars(
        select(KnowledgeDocument).where(KnowledgeDocument.base_id == base_id).limit(200)
    )
    result = []
    for row in rows:
        try:
            await document_access(session, row.id, user, P.KNOWLEDGE_READ)
        except PermissionDeniedError:
            continue
        result.append({"id": row.id, "code": row.code, "title": row.title, "kind": row.kind})
    return result


@router.post("/documents/{document_id}/versions", status_code=201)
async def create_version(
    document_id: UUID, payload: VersionCreate, request: Request, session: SessionDep, user: Uploader
):
    version, grant = await service.create_version(
        session,
        user,
        document_id,
        payload,
        request.app.state.settings,
        request.app.state.providers.storage,
    )
    return {"version": version_view(version), "upload": grant.model_dump()}


@router.get("/documents/{document_id}/versions")
async def versions(document_id: UUID, session: SessionDep, user: Reader):
    await document_access(session, document_id, user, P.KNOWLEDGE_READ)
    rows = await session.scalars(
        select(KnowledgeVersion)
        .where(KnowledgeVersion.document_id == document_id)
        .order_by(KnowledgeVersion.number.desc())
    )
    return [version_view(v) for v in rows]


@router.post("/versions/{version_id}/uploads:complete")
async def complete(version_id: UUID, request: Request, session: SessionDep, user: Uploader):
    return version_view(
        await service.complete_upload(
            session, user, version_id, request.app.state.providers.storage
        )
    )


@router.post("/versions/{version_id}:process", status_code=202)
async def process(version_id: UUID, request: Request, session: SessionDep, user: Indexer):
    version, _, base = await version_access(session, version_id, user, P.KNOWLEDGE_INDEX)
    await session.refresh(version, with_for_update=True)
    if version.status not in {"uploaded", "failed", "processing"}:
        raise ConflictError("version cannot be processed in its current state")
    return await start_knowledge_job(
        session,
        user,
        version,
        "index",
        organization_unit_id=base.organization_unit_id,
        settings=request.app.state.settings,
        providers=request.app.state.providers,
        knowledge_manager=request.app.state.knowledge,
        session_factory=request.app.state.database.session_factory,
    )


@router.get("/versions/{version_id}/evidence")
async def evidence(version_id: UUID, session: SessionDep, user: Reader):
    await version_access(session, version_id, user, P.KNOWLEDGE_READ)
    chunks = await session.scalars(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.version_id == version_id)
        .order_by(KnowledgeChunk.ordinal)
        .limit(1000)
    )
    return [
        {"id": c.id, "text": c.text, "locator": c.locator, "ordinal": c.ordinal} for c in chunks
    ]


@router.get("/versions/{version_id}/artifact")
async def artifact(version_id: UUID, session: SessionDep, user: Reviewer):
    version, _, _ = await version_access(session, version_id, user, P.KNOWLEDGE_REVIEW)
    return {"version": version_view(version), "artifact": version.artifact}


@router.post("/versions/{version_id}/artifact:correct")
async def correct_artifact(
    version_id: UUID, payload: ArtifactCorrection, session: SessionDep, user: Editor
):
    return version_view(await service.correct_artifact(session, user, version_id, payload))


@router.post("/versions/{version_id}:review")
async def review(version_id: UUID, payload: Review, session: SessionDep, user: Reviewer):
    return version_view(
        await service.review_version(session, user, version_id, payload.approved, payload.reason)
    )


@router.post("/versions/{version_id}:publish")
async def publish(version_id: UUID, session: SessionDep, user: Publisher):
    return version_view(await service.publish_version(session, user, version_id))


@router.post("/versions/{version_id}:withdraw")
async def withdraw(version_id: UUID, payload: Reason, session: SessionDep, user: Withdrawer):
    return version_view(await service.withdraw_version(session, user, version_id, payload.reason))


@router.post("/search")
async def search(payload: SearchInput, request: Request, session: SessionDep, user: Reader):
    from app.shared.platform.runtime import provider_from_snapshot

    snapshot = await knowledge_snapshot(session, request.app.state.settings)
    async with request.app.state.knowledge.scoped(snapshot) as knowledge:
        evidence, trace = await retrieve(
            session,
            user,
            payload,
            knowledge,
            provider_from_snapshot(request.app.state.providers.text, snapshot),
        )
    return {"evidence": evidence, "trace": trace}


@router.post("/runs", status_code=202)
async def ask(payload: SearchInput, request: Request, session: SessionDep, user: Reader):
    if payload.conversation_id:
        from app.modules.knowledge.domain.models import KnowledgeConversation

        conversation = await session.get(KnowledgeConversation, payload.conversation_id)
        if conversation is None or conversation.created_by != user.user_id:
            raise NotFoundError("knowledge_conversation", payload.conversation_id)
    run = KnowledgeRun(
        requested_by=user.user_id,
        question=payload.question,
        conversation_id=payload.conversation_id,
        options=payload.model_dump(mode="json"),
    )
    session.add(run)
    await session.flush()
    return await start_knowledge_job(
        session,
        user,
        run,
        "answer",
        settings=request.app.state.settings,
        providers=request.app.state.providers,
        knowledge_manager=request.app.state.knowledge,
        session_factory=request.app.state.database.session_factory,
    )


@router.get("/runs/{run_id}")
async def read_run(run_id: UUID, session: SessionDep, user: Reader):
    run = await run_access(session, user, run_id)
    return {"id": run.id, "status": run.status, "answer": run.answer, "trace": run.trace}


@router.post("/runs/{run_id}/feedback", status_code=201)
async def feedback(run_id: UUID, payload: FeedbackInput, session: SessionDep, user: Reader):
    await run_access(session, user, run_id)
    entry = KnowledgeFeedback(run_id=run_id, created_by=user.user_id, **payload.model_dump())
    session.add(entry)
    await session.commit()
    return {"id": entry.id}


@router.post("/evaluations", status_code=202)
async def evaluate(
    payload: EvaluationCreate, request: Request, session: SessionDep, user: Evaluator
):
    evaluation = KnowledgeEvaluation(
        requested_by=user.user_id, dataset=payload.model_dump(mode="json")
    )
    session.add(evaluation)
    await session.flush()
    return await start_knowledge_job(
        session,
        user,
        evaluation,
        "evaluate",
        settings=request.app.state.settings,
        providers=request.app.state.providers,
        knowledge_manager=request.app.state.knowledge,
        session_factory=request.app.state.database.session_factory,
    )


@router.get("/runs/{run_id}/events")
async def events(
    run_id: UUID,
    request: Request,
    session: SessionDep,
    user: Reader,
    after: int = Query(default=0, ge=0),
    last_event_id: int | None = Header(default=None, ge=0),
):
    from app.modules.knowledge.domain.models import KnowledgeRunEvent

    await run_access(session, user, run_id)
    await session.commit()

    async def stream():
        cursor = max(after, last_event_id or 0)
        for _ in range(600):
            if await request.is_disconnected():
                return
            async with request.app.state.database.session_factory() as current:
                run = await current.get(KnowledgeRun, run_id)
                job = await current.get(AsyncJob, run.job_id)
                try:
                    await _verify_token(
                        request,
                        HTTPAuthorizationCredentials(
                            scheme="Bearer",
                            credentials=request.headers["Authorization"].split(" ", 1)[1],
                        ),
                    )
                    fresh_user = await job_user(current, job)
                    await run_access(current, fresh_user, run_id)
                except AppError:
                    yield 'event: error\ndata: {"code":"access_revoked"}\n\n'
                    return
                rows = list(
                    await current.scalars(
                        select(KnowledgeRunEvent)
                        .where(
                            KnowledgeRunEvent.run_id == run_id, KnowledgeRunEvent.sequence > cursor
                        )
                        .order_by(KnowledgeRunEvent.sequence)
                        .limit(100)
                    )
                )
                for row in rows:
                    cursor = row.sequence
                    data = json.dumps(row.payload, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {row.kind}\ndata: {data}\n\n"
                if run.status in {"succeeded", "failed", "cancelled"} and len(rows) < 100:
                    yield f"event: state\ndata: {json.dumps({'status': run.status})}\n\n"
                    return
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/bases/{base_id}/acl")
async def base_acl(base_id: UUID, payload: ACLUpdate, session: SessionDep, user: Editor):
    base = await base_access(session, base_id, user, P.KNOWLEDGE_EDIT)
    await session.refresh(base, with_for_update=True)
    if base.acl_version != payload.expected_version:
        raise ConflictError("ACL version changed")
    base.reader_ids = [str(i) for i in payload.reader_ids]
    base.acl_version += 1
    await add_audit(
        session,
        user=user,
        action="knowledge.acl.update",
        resource_type="knowledge_base",
        resource_id=base.id,
        after={"reader_ids": base.reader_ids, "acl_version": base.acl_version},
    )
    await session.commit()
    return {"id": base.id, "acl_version": base.acl_version}


@router.get("/evaluations/{evaluation_id}")
async def evaluation_result(evaluation_id: UUID, session: SessionDep, user: Evaluator):
    evaluation = await session.get(KnowledgeEvaluation, evaluation_id)
    if evaluation is None or evaluation.requested_by != user.user_id:
        raise NotFoundError("knowledge_evaluation", evaluation_id)
    # Results include evidence identifiers, and must follow current source authorization too.
    for base_id in evaluation.dataset.get("base_ids", []):
        await base_access(session, UUID(base_id), user, P.KNOWLEDGE_READ)
    return {"id": evaluation.id, "status": evaluation.status, "results": evaluation.results}


@router.get("/bases")
async def bases(session: SessionDep, user: Reader):
    rows = list(
        await session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at).limit(500))
    )
    result = []
    for row in rows:
        try:
            await base_access(session, row.id, user, P.KNOWLEDGE_READ)
        except PermissionDeniedError:
            continue
        result.append({"id": row.id, "name": row.name, "acl_version": row.acl_version})
    return result


@router.post("/documents/{document_id}/acl")
async def document_acl(document_id: UUID, payload: ACLUpdate, session: SessionDep, user: Editor):
    document, _ = await document_access(session, document_id, user, P.KNOWLEDGE_EDIT)
    await session.refresh(document, with_for_update=True)
    if document.acl_version != payload.expected_version:
        raise ConflictError("ACL version changed")
    from app.modules.knowledge.application.access import meeting_reader_scope

    source_readers = await meeting_reader_scope(session, document.id)
    proposed = {str(i) for i in payload.reader_ids}
    if source_readers is not None and (not proposed or not proposed <= source_readers):
        raise ConflictError("knowledge readers cannot exceed the source meeting scope")
    document.reader_ids = [str(i) for i in payload.reader_ids]
    document.acl_version += 1
    await add_audit(
        session,
        user=user,
        action="knowledge.acl.update",
        resource_type="knowledge_document",
        resource_id=document.id,
        after={"reader_ids": document.reader_ids, "acl_version": document.acl_version},
    )
    await session.commit()
    return {"id": document.id, "acl_version": document.acl_version}


@router.post("/conversations", status_code=201)
async def conversation(payload: ConversationCreate, session: SessionDep, user: Reader):
    row = KnowledgeConversation(created_by=user.user_id, title=payload.title)
    session.add(row)
    await session.commit()
    return {"id": row.id, "title": row.title}


@router.get("/conversations/{conversation_id}/runs")
async def conversation_runs(conversation_id: UUID, session: SessionDep, user: Reader):
    conversation = await session.get(KnowledgeConversation, conversation_id)
    if conversation is None or conversation.created_by != user.user_id:
        raise NotFoundError("knowledge_conversation", conversation_id)
    rows = list(
        await session.scalars(
            select(KnowledgeRun)
            .where(KnowledgeRun.conversation_id == conversation_id)
            .order_by(KnowledgeRun.created_at)
            .limit(100)
        )
    )
    result = []
    for row in rows:
        try:
            row = await run_access(session, user, row.id)
            result.append(
                {"id": row.id, "question": row.question, "status": row.status, "answer": row.answer}
            )
        except PermissionDeniedError:
            result.append({"id": row.id, "status": "access_revoked"})
    return result


@router.post("/runs/{run_id}:cancel")
async def cancel(run_id: UUID, session: SessionDep, user: Reader):
    run = await run_access(session, user, run_id)
    job = await session.get(AsyncJob, run.job_id, with_for_update=True)
    if job is None:
        raise ConflictError("run has no execution job")
    await request_cancel(session, job)
    if job.status == "queued":
        await update_job(session, job, status="cancelled", progress=0)
        run.status = "cancelled"
    await session.commit()
    return {"id": run.id, "status": run.status, "cancel_requested": job.cancel_requested}


@router.get("/versions/{version_id}/original")
async def original(version_id: UUID, request: Request, session: SessionDep, user: Reader):
    from fastapi.security import HTTPAuthorizationCredentials

    from app.shared.media.preview import private_download
    from app.shared.security.authentication.dependencies import _verify_token
    from app.shared.security.authorization.repository import load_current_user
    from app.shared.security.identity.repository import resolve_identity

    version, _, _ = await version_access(session, version_id, user, P.KNOWLEDGE_READ)
    object_key = version.object_key
    await session.commit()

    async def authorize_download():
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=request.headers["Authorization"].split(" ", 1)[1]
        )
        token = await _verify_token(request, credentials)
        async with request.app.state.database.session_factory() as current:
            identity = await resolve_identity(current, token)
            fresh = await load_current_user(current, identity)
            await version_access(current, version_id, fresh, P.KNOWLEDGE_READ)

    return await private_download(
        request.app.state.providers.storage, object_key, authorize_download
    )


@router.get("/versions/{version_id}/diff/{other_id}")
async def version_diff(version_id: UUID, other_id: UUID, session: SessionDep, user: Reader):
    import difflib

    before, _, _ = await version_access(session, version_id, user, P.KNOWLEDGE_READ)
    after, _, _ = await version_access(session, other_id, user, P.KNOWLEDGE_READ)
    if before.document_id != after.document_id:
        raise ConflictError("diff requires versions of the same document")
    texts = []
    for version in (before, after):
        rows = await session.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.version_id == version.id)
            .order_by(KnowledgeChunk.ordinal)
        )
        texts.append("\n".join(row.text for row in rows))
    if any(len(text) > 500000 for text in texts):
        raise ConflictError("document exceeds interactive diff limit")
    return {
        "before": before.number,
        "after": after.number,
        "diff": list(
            difflib.unified_diff(
                texts[0].splitlines(),
                texts[1].splitlines(),
                fromfile=str(before.number),
                tofile=str(after.number),
                lineterm="",
            )
        ),
    }


@router.post("/evaluations/{evaluation_id}:review")
async def post_evaluation_review(
    evaluation_id: UUID, payload: EvaluationExpertReview, session: SessionDep, user: Evaluator
):
    evaluation = await review_evaluation(session, user, evaluation_id, payload)
    return {
        "id": evaluation.id,
        "status": evaluation.status,
        "results": evaluation.results,
        "review_revision": evaluation.options["review_revision"],
    }

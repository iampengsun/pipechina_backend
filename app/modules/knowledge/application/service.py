import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.modules.knowledge.application.access import base_access, document_access, version_access
from app.modules.knowledge.domain.models import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeFact,
    KnowledgeVersion,
)
from app.modules.knowledge.domain.schemas import BaseCreate, DocumentCreate, VersionCreate
from app.shared.errors import ConflictError
from app.shared.platform.models import AsyncJob, UploadSession
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions as P


async def create_base(session, user, payload: BaseCreate):
    org = payload.organization_unit_id or user.organization_unit_id
    require_data_scope(user, org, P.KNOWLEDGE_EDIT, owner_id=user.user_id)
    base = KnowledgeBase(
        name=payload.name,
        organization_unit_id=org,
        created_by=user.user_id,
        reader_ids=[str(i) for i in payload.reader_ids],
    )
    session.add(base)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="knowledge.base.create",
        resource_type="knowledge_base",
        resource_id=base.id,
    )
    await session.commit()
    return base


async def create_document(session, user, payload: DocumentCreate):
    await base_access(session, payload.base_id, user, P.KNOWLEDGE_EDIT)
    existing = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.base_id == payload.base_id, KnowledgeDocument.code == payload.code
        )
    )
    if existing:
        raise ConflictError("document code already exists")
    document = KnowledgeDocument(**payload.model_dump(), created_by=user.user_id)
    session.add(document)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="knowledge.document.create",
        resource_type="knowledge_document",
        resource_id=document.id,
    )
    await session.commit()
    return document


async def create_version(session, user, document_id, payload: VersionCreate, settings, storage):
    document, base = await document_access(session, document_id, user, P.KNOWLEDGE_UPLOAD)
    await session.refresh(document, with_for_update=True)
    duplicate = await session.scalar(
        select(KnowledgeVersion).where(
            KnowledgeVersion.document_id == document_id, KnowledgeVersion.sha256 == payload.sha256
        )
    )
    if duplicate:
        raise ConflictError(
            "identical content already exists for document", version_id=str(duplicate.id)
        )
    number = (
        await session.scalar(
            select(func.max(KnowledgeVersion.number)).where(
                KnowledgeVersion.document_id == document_id
            )
        )
        or 0
    ) + 1
    from app.modules.knowledge.application.configuration import knowledge_snapshot

    snapshot = await knowledge_snapshot(session, settings)
    generation = (
        "g" + hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:24]
    )
    version = KnowledgeVersion(
        document_id=document_id,
        number=number,
        **payload.model_dump(),
        object_key="pending",
        generation=generation,
        snapshot=snapshot,
        created_by=user.user_id,
    )
    session.add(version)
    await session.flush()
    version.object_key = f"knowledge/{base.id}/{document.id}/{version.id}/original"
    grant = await storage.create_upload(
        object_key=version.object_key, mime_type=version.mime_type, size_bytes=version.size_bytes
    )
    upload = UploadSession(
        resource_type="knowledge_version",
        resource_id=version.id,
        organization_unit_id=base.organization_unit_id,
        object_key=version.object_key,
        filename=version.filename,
        mime_type=version.mime_type,
        size_bytes=version.size_bytes,
        client_sha256=version.sha256,
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(seconds=grant.expires_in),
    )
    session.add(upload)
    await add_audit(
        session,
        user=user,
        action="knowledge.version.create",
        resource_type="knowledge_version",
        resource_id=version.id,
    )
    await session.commit()
    return version, grant


async def complete_upload(session, user, version_id, storage):
    version, _, _ = await version_access(session, version_id, user, P.KNOWLEDGE_UPLOAD)
    await session.refresh(version, with_for_update=True)
    if version.status != "uploading":
        raise ConflictError("version is not awaiting upload")
    upload = await session.scalar(
        select(UploadSession)
        .where(
            UploadSession.resource_type == "knowledge_version",
            UploadSession.resource_id == version_id,
        )
        .with_for_update()
    )
    if upload is None or upload.expires_at < datetime.now(UTC):
        raise ConflictError("upload session expired")
    meta = await storage.head(version.object_key)
    if meta.size_bytes != version.size_bytes or meta.mime_type != version.mime_type:
        raise ConflictError("uploaded object metadata mismatch")
    # Read the actual object; never trust a client-provided checksum or opaque S3 ETag.
    import asyncio

    async with storage.materialize(version.object_key) as path:

        def digest():
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()

        actual = await asyncio.to_thread(digest)
    if actual != version.sha256:
        raise ConflictError("uploaded object checksum mismatch")
    upload.status = "verified"
    version.status = "uploaded"
    await add_audit(
        session,
        user=user,
        action="knowledge.upload.complete",
        resource_type="knowledge_version",
        resource_id=version.id,
    )
    await session.commit()
    return version


async def review_version(session, user, version_id, approved, reason):
    version, _, _ = await version_access(session, version_id, user, P.KNOWLEDGE_REVIEW)
    await session.refresh(version, with_for_update=True)
    if version.status != "review":
        raise ConflictError("version is not awaiting review")
    version.status = "approved" if approved else "rejected"
    version.reviewed_by, version.review_reason = user.user_id, reason
    await add_audit(
        session,
        user=user,
        action="knowledge.review",
        resource_type="knowledge_version",
        resource_id=version.id,
        after={"approved": approved},
        reason=reason,
    )
    await session.commit()
    return version


async def publish_version(session, user, version_id):
    version, document, _ = await version_access(session, version_id, user, P.KNOWLEDGE_PUBLISH)
    await session.refresh(document, with_for_update=True)
    await session.refresh(version, with_for_update=True)
    if version.status != "approved" or not all(
        version.index_state.get(k) == "ready" for k in ("fulltext", "vector", "graph")
    ):
        raise ConflictError("review and all three indexes must be complete before publication")
    others = list(
        await session.scalars(
            select(KnowledgeVersion)
            .where(
                KnowledgeVersion.document_id == document.id,
                KnowledgeVersion.status == "published",
                KnowledgeVersion.id != version.id,
            )
            .with_for_update()
        )
    )
    for previous in others:
        if previous.effective_from >= version.effective_from:
            raise ConflictError("new publication must start after existing published versions")
        if previous.effective_to is None or previous.effective_to > version.effective_from:
            previous.effective_to = version.effective_from
    version.status = "published"
    await add_audit(
        session,
        user=user,
        action="knowledge.publish",
        resource_type="knowledge_version",
        resource_id=version.id,
    )
    await session.commit()
    return version


async def withdraw_version(session, user, version_id, reason):
    version, _, _ = await version_access(session, version_id, user, P.KNOWLEDGE_WITHDRAW)
    await session.refresh(version, with_for_update=True)
    if version.status != "published":
        raise ConflictError("only published versions can be withdrawn")
    version.status = "withdrawn"
    from app.modules.meeting.domain.models import Meeting, MeetingKnowledgePublication

    publication = await session.scalar(
        select(MeetingKnowledgePublication).where(
            MeetingKnowledgePublication.knowledge_version_id == version.id
        )
    )
    if publication is not None:
        meeting = await session.get(Meeting, publication.meeting_id, with_for_update=True)
        if meeting is not None:
            publications = list(
                await session.scalars(
                    select(MeetingKnowledgePublication).where(
                        MeetingKnowledgePublication.meeting_id == meeting.id
                    )
                )
            )
            active = False
            for item in publications:
                linked = await session.get(KnowledgeVersion, item.knowledge_version_id)
                if linked is not None and linked.status not in {"withdrawn", "rejected"}:
                    active = True
                    break
            if not active:
                meeting.status = "withdrawn"
    await add_audit(
        session,
        user=user,
        action="knowledge.withdraw",
        resource_type="knowledge_version",
        resource_id=version.id,
        reason=reason,
    )
    # Immediate visibility gate; physical indexes are not the authority for publication.
    await session.commit()
    return version


async def correct_artifact(session, user, version_id, payload):
    version, _, _ = await version_access(session, version_id, user, P.KNOWLEDGE_EDIT)
    await session.refresh(version, with_for_update=True)
    if version.status not in {"failed", "review", "rejected"}:
        raise ConflictError("only unpublished processed artifacts can be corrected")
    active = await session.scalar(
        select(AsyncJob.id).where(
            AsyncJob.resource_id == version_id,
            AsyncJob.resource_type == "knowledge_version",
            AsyncJob.status.in_(["queued", "running"]),
        )
    )
    if active:
        raise ConflictError("wait for active processing to finish before correction")
    import copy

    artifact = copy.deepcopy(version.artifact)
    by_id = {b["id"]: b for b in artifact.get("blocks", [])}
    if len({c.block_id for c in payload.corrections}) != len(payload.corrections):
        raise ConflictError("duplicate correction block IDs")
    changes = []
    for correction in payload.corrections:
        block = by_id.get(correction.block_id)
        if block is None:
            raise ConflictError("unknown parsed block")
        changes.append(
            {"block_id": correction.block_id, "before": block["text"], "after": correction.text}
        )
        block["text"] = correction.text
        block.setdefault("locator", {})["human_corrected"] = True
    artifact.setdefault("corrections", []).append(
        {
            "by": str(user.user_id),
            "at": datetime.now(UTC).isoformat(),
            "reason": payload.reason,
            "changes": changes,
        }
    )
    version.artifact = artifact
    version.index_state = {"cleanup_required": True}
    version.status = "uploaded"
    version.reviewed_by, version.review_reason = None, None
    await session.execute(delete(KnowledgeFact).where(KnowledgeFact.version_id == version_id))
    await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.version_id == version_id))
    await add_audit(
        session,
        user=user,
        action="knowledge.artifact.correct",
        resource_type="knowledge_version",
        resource_id=version_id,
        after={"block_ids": [c.block_id for c in payload.corrections]},
        reason=payload.reason,
    )
    await session.commit()
    return version

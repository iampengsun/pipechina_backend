from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.domain.models import KnowledgeBase, KnowledgeDocument, KnowledgeVersion
from app.modules.knowledge.domain.schemas import SearchInput
from app.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.security.authorization.dependencies import data_scope_clause, require_data_scope
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.authorization.schemas import CurrentUser


def authorize(
    user: CurrentUser, base: KnowledgeBase, document: KnowledgeDocument | None, permission: str
) -> None:
    require_data_scope(
        user,
        base.organization_unit_id,
        permission,
        owner_id=document.created_by if document else base.created_by,
    )
    if user.has_global_permission(P.ALL):
        return
    for readers in (base.reader_ids, document.reader_ids if document else []):
        if readers and str(user.user_id) not in readers:
            raise PermissionDeniedError(permission)


async def base_access(session: AsyncSession, base_id: UUID, user: CurrentUser, permission: str):
    base = await session.get(KnowledgeBase, base_id, populate_existing=True)
    if base is None:
        raise NotFoundError("knowledge_base", base_id)
    authorize(user, base, None, permission)
    return base


async def document_access(
    session: AsyncSession, document_id: UUID, user: CurrentUser, permission: str
):
    document = await session.get(KnowledgeDocument, document_id, populate_existing=True)
    if document is None:
        raise NotFoundError("knowledge_document", document_id)
    base = await session.get(KnowledgeBase, document.base_id, populate_existing=True)
    if base is None:
        raise NotFoundError("knowledge_base", document.base_id)
    authorize(user, base, document, permission)
    await authorize_meeting_source(session, user, document.id, permission)
    return document, base


async def version_access(
    session: AsyncSession, version_id: UUID, user: CurrentUser, permission: str
):
    version = await session.get(KnowledgeVersion, version_id, populate_existing=True)
    if version is None:
        raise NotFoundError("knowledge_version", version_id)
    document, base = await document_access(session, version.document_id, user, permission)
    if permission == P.KNOWLEDGE_READ and version.status != "published":
        for editorial_permission in (P.KNOWLEDGE_EDIT, P.KNOWLEDGE_INDEX, P.KNOWLEDGE_REVIEW):
            try:
                authorize(user, base, document, editorial_permission)
                break
            except PermissionDeniedError:
                continue
        else:
            raise PermissionDeniedError(P.KNOWLEDGE_READ)
    return version, document, base


async def visible_versions(session: AsyncSession, user: CurrentUser, options: SearchInput):
    if not user.has_permission(P.KNOWLEDGE_READ):
        raise PermissionDeniedError(P.KNOWLEDGE_READ)
    statement = (
        select(KnowledgeVersion, KnowledgeDocument, KnowledgeBase)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeVersion.document_id)
        .join(KnowledgeBase, KnowledgeBase.id == KnowledgeDocument.base_id)
        .where(
            KnowledgeVersion.status == "published",
            data_scope_clause(
                user,
                P.KNOWLEDGE_READ,
                KnowledgeBase.organization_unit_id,
                owner_column=KnowledgeDocument.created_by,
            ),
        )
        .execution_options(populate_existing=True)
    )
    if options.base_ids:
        statement = statement.where(KnowledgeBase.id.in_(options.base_ids))
    if options.version_ids:
        statement = statement.where(KnowledgeVersion.id.in_(options.version_ids))
    else:
        at = options.as_of or datetime.now(UTC)
        statement = statement.where(
            KnowledgeVersion.effective_from <= at,
            (KnowledgeVersion.effective_to.is_(None)) | (KnowledgeVersion.effective_to > at),
        )
    visible = []
    for version, document, base in (await session.execute(statement)).all():
        try:
            authorize(user, base, document, P.KNOWLEDGE_READ)
            await authorize_meeting_source(session, user, document.id, P.KNOWLEDGE_READ)
        except PermissionDeniedError:
            continue
        if (
            options.equipment_model
            and version.equipment_models
            and options.equipment_model not in version.equipment_models
        ):
            continue
        visible.append((version, document, base))
    if len(visible) > 2000:
        raise ConflictError("narrow the knowledge bases or time range; version scope too large")
    return visible


async def meeting_reader_scope(session, document_id):
    from app.modules.meeting.domain.models import Meeting, MeetingKnowledgePublication

    rows = await session.scalars(
        select(Meeting)
        .join(MeetingKnowledgePublication, MeetingKnowledgePublication.meeting_id == Meeting.id)
        .where(MeetingKnowledgePublication.knowledge_document_id == document_id)
    )
    scopes = [set(row.reader_ids) | {str(row.created_by)} for row in rows if row.reader_ids]
    return set.intersection(*scopes) if scopes else None


async def authorize_meeting_source(session, user, document_id, permission):
    if user.has_global_permission(P.ALL):
        return
    readers = await meeting_reader_scope(session, document_id)
    if readers is not None and str(user.user_id) not in readers:
        raise PermissionDeniedError(permission)

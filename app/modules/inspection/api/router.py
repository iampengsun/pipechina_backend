from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from app.modules.inspection.application.service import (
    analyze_inspection,
    complete_image,
    create_image,
    create_inspection,
    get_finding,
    get_inspection,
    link_finding_to_event,
    start_finding_workflow,
)
from app.modules.inspection.domain.models import (
    InspectionFinding,
    InspectionFindingLink,
    InspectionImage,
    InspectionRecord,
)
from app.modules.inspection.domain.schemas import (
    FindingLinkEvent,
    FindingReview,
    ImageComplete,
    InspectionCreate,
    InspectionImageCreate,
    InspectionView,
    RevokeLinkInput,
)
from app.shared.db import SessionDep
from app.shared.errors import ConflictError, NotFoundError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import (
    data_scope_clause,
    require_data_scope,
    require_permission,
)
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(tags=["inspection"])


@router.post("/inspections", response_model=InspectionView, status_code=201)
async def post_inspection(
    payload: InspectionCreate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_CREATE))],
) -> InspectionView:
    return InspectionView.model_validate(await create_inspection(session, payload, user))


@router.get("/inspections", response_model=list[InspectionView])
async def list_inspections(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_READ))],
) -> list[InspectionView]:
    statement = select(InspectionRecord).order_by(InspectionRecord.inspected_at.desc()).limit(100)
    statement = statement.where(
        data_scope_clause(
            user,
            Permissions.INSPECTION_READ,
            InspectionRecord.organization_unit_id,
            owner_column=InspectionRecord.created_by,
        )
    )
    rows = await session.scalars(statement)
    return [InspectionView.model_validate(row) for row in rows]


@router.post("/inspections/{inspection_id}/images", status_code=201)
async def post_image(
    inspection_id: UUID,
    payload: InspectionImageCreate,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_UPLOAD))],
) -> dict:
    settings = request.app.state.settings
    image, grant = await create_image(
        session,
        inspection=await get_inspection(session, inspection_id),
        payload=payload,
        user=user,
        storage=request.app.state.providers.storage,
        max_bytes=settings.max_image_bytes,
        allowed_types=settings.allowed_image_types,
    )
    return {
        "image_id": str(image.id),
        "object_key": grant.object_key,
        "upload_url": grant.upload_url,
        "headers": grant.headers,
    }


@router.post("/inspections/{inspection_id}/images/{image_id}:complete")
async def post_image_complete(
    inspection_id: UUID,
    image_id: UUID,
    payload: ImageComplete,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_UPLOAD))],
) -> dict:
    inspection = await get_inspection(session, inspection_id)
    image = await session.get(InspectionImage, image_id)
    if image is None or image.inspection_id != inspection.id:
        raise NotFoundError("inspection_image", image_id)
    await complete_image(
        session,
        inspection=inspection,
        image=image,
        user=user,
        storage=request.app.state.providers.storage,
        checksum=payload.checksum,
    )
    return {"image_id": str(image.id), "status": image.status}


@router.delete("/inspections/{inspection_id}/images/{image_id}", status_code=204)
async def delete_image(
    inspection_id: UUID,
    image_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_DELETE))],
) -> Response:
    inspection = await get_inspection(session, inspection_id)
    require_data_scope(
        user,
        inspection.organization_unit_id,
        Permissions.INSPECTION_DELETE,
        owner_id=inspection.created_by,
    )
    image = await session.get(InspectionImage, image_id)
    if image is None or image.inspection_id != inspection.id or image.deleted:
        raise NotFoundError("inspection_image", image_id)
    confirmed = await session.scalar(
        select(InspectionFinding.id).where(
            InspectionFinding.image_id == image.id,
            InspectionFinding.review_status == "confirmed",
        )
    )
    if confirmed:
        raise ConflictError("image with confirmed finding is protected from deletion")
    image.deleted = True
    image.deleted_by = user.user_id
    image.deleted_at = datetime.now(UTC)
    image.status = "deleted"
    await request.app.state.providers.storage.delete(image.object_key)
    await add_audit(
        session,
        user=user,
        action="inspection_image.soft_delete",
        resource_type="inspection_image",
        resource_id=image.id,
    )
    await session.commit()
    return Response(status_code=204)


@router.post("/inspections/{inspection_id}:analyze", status_code=202)
async def post_analyze(
    inspection_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_ANALYZE))],
) -> dict:
    job_id, findings = await analyze_inspection(
        session,
        inspection=await get_inspection(session, inspection_id),
        user=user,
        provider=request.app.state.providers.vision,
        inline=request.app.state.settings.run_tasks_inline,
    )
    return {"job_id": str(job_id), "finding_ids": [str(item.id) for item in findings]}


@router.get("/inspections/{inspection_id}")
async def read_inspection(
    inspection_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_READ))],
) -> dict:
    inspection = await get_inspection(session, inspection_id)
    require_data_scope(
        user,
        inspection.organization_unit_id,
        Permissions.INSPECTION_READ,
        owner_id=inspection.created_by,
    )
    images = list(
        await session.scalars(
            select(InspectionImage).where(
                InspectionImage.inspection_id == inspection.id, InspectionImage.deleted.is_(False)
            )
        )
    )
    findings = list(
        await session.scalars(
            select(InspectionFinding).where(InspectionFinding.inspection_id == inspection.id)
        )
    )
    return {
        "inspection": InspectionView.model_validate(inspection).model_dump(mode="json"),
        "images": [
            {"id": str(item.id), "filename": item.filename, "status": item.status}
            for item in images
        ],
        "findings": [
            {
                "id": str(item.id),
                "title": item.title,
                "severity": item.severity,
                "review_status": item.review_status,
                "confidence": item.confidence,
            }
            for item in findings
        ],
    }


async def _review_finding(
    finding_id: UUID,
    target: str,
    payload: FindingReview,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    finding = await get_finding(session, finding_id)
    inspection = await get_inspection(session, finding.inspection_id)
    require_data_scope(
        user,
        inspection.organization_unit_id,
        Permissions.INSPECTION_REVIEW,
        owner_id=inspection.created_by,
    )
    if finding.review_status != "pending":
        raise ConflictError("finding is not pending review")
    finding.review_status = target
    finding.reviewed_by = user.user_id
    finding.review_reason = payload.reason
    await add_audit(
        session,
        user=user,
        action=f"inspection_finding.{target}",
        resource_type="inspection_finding",
        resource_id=finding.id,
        reason=payload.reason,
    )
    await session.commit()
    return {"id": str(finding.id), "review_status": finding.review_status}


@router.post("/findings/{finding_id}:confirm")
async def confirm_finding(
    finding_id: UUID,
    payload: FindingReview,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_REVIEW))],
) -> dict:
    return await _review_finding(finding_id, "confirmed", payload, session, user)


@router.post("/findings/{finding_id}:reject")
async def reject_finding(
    finding_id: UUID,
    payload: FindingReview,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_REVIEW))],
) -> dict:
    return await _review_finding(finding_id, "rejected", payload, session, user)


@router.post("/findings/{finding_id}:link-event", status_code=201)
async def post_link_event(
    finding_id: UUID,
    payload: FindingLinkEvent,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_LINK))],
) -> dict:
    link = await link_finding_to_event(
        session, await get_finding(session, finding_id), payload.event_id, user
    )
    return {"link_id": str(link.id)}


@router.get("/findings/{finding_id}/links")
async def read_links(
    finding_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_READ))],
) -> list[dict]:
    finding = await get_finding(session, finding_id)
    inspection = await get_inspection(session, finding.inspection_id)
    require_data_scope(
        user,
        inspection.organization_unit_id,
        Permissions.INSPECTION_READ,
        owner_id=inspection.created_by,
    )
    rows = await session.scalars(
        select(InspectionFindingLink).where(InspectionFindingLink.finding_id == finding.id)
    )
    return [
        {
            "id": str(row.id),
            "target_type": row.target_type,
            "target_id": str(row.target_id),
            "active": row.active,
            "revoke_reason": row.revoke_reason,
        }
        for row in rows
    ]


@router.delete("/findings/{finding_id}/links/{link_id}", status_code=204)
async def delete_link(
    finding_id: UUID,
    link_id: UUID,
    payload: RevokeLinkInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_LINK))],
) -> Response:
    finding = await get_finding(session, finding_id)
    inspection = await get_inspection(session, finding.inspection_id)
    require_data_scope(
        user,
        inspection.organization_unit_id,
        Permissions.INSPECTION_LINK,
        owner_id=inspection.created_by,
    )
    link = await session.get(InspectionFindingLink, link_id)
    if link is None or link.finding_id != finding.id:
        raise NotFoundError("inspection_finding_link", link_id)
    if not link.active:
        raise ConflictError("finding link is already inactive")
    link.active = False
    link.revoked_by = user.user_id
    link.revoked_at = datetime.now(UTC)
    link.revoke_reason = payload.reason
    await session.commit()
    return Response(status_code=204)


@router.post("/findings/{finding_id}:start-workflow", status_code=202)
async def post_start_workflow(
    finding_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.INSPECTION_WORKFLOW))],
) -> dict:
    job_id, workflow_id = await start_finding_workflow(
        session,
        finding=await get_finding(session, finding_id),
        user=user,
        provider=request.app.state.providers.text,
        inline=request.app.state.settings.run_tasks_inline,
        knowledge=request.app.state.knowledge,
    )
    return {"job_id": str(job_id), "workflow_id": str(workflow_id) if workflow_id else None}

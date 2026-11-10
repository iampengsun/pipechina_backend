from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.modules.report.application.service import (
    create_export,
    create_report,
    generate_report,
    get_report,
    restore_report_version,
    update_report_content,
)
from app.modules.report.domain.models import ReportExport, ReportVersion
from app.modules.report.domain.schemas import (
    ReportContentUpdate,
    ReportCreate,
    ReportExportCreate,
    ReportReviewInput,
    ReportView,
    ReportWithdrawInput,
)
from app.shared.db import SessionDep
from app.shared.errors import ConflictError, NotFoundError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.dependencies import require_data_scope, require_permission
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser

router = APIRouter(tags=["report"])


@router.post("/reports", response_model=ReportView, status_code=201)
async def post_report(
    payload: ReportCreate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_CREATE))],
) -> ReportView:
    return ReportView.model_validate(await create_report(session, payload, user))


@router.post("/reports/{report_id}:generate", status_code=202)
async def post_generate(
    report_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_GENERATE))],
) -> dict:
    job_id = await generate_report(
        session,
        report=await get_report(session, report_id),
        user=user,
        provider=request.app.state.providers.text,
        inline=request.app.state.settings.run_tasks_inline,
    )
    return {"job_id": str(job_id)}


@router.get("/reports/{report_id}")
async def read_report(
    report_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_READ))],
) -> dict:
    report = await get_report(session, report_id)
    require_data_scope(
        user,
        report.organization_unit_id,
        Permissions.REPORT_READ,
        owner_id=report.created_by,
    )
    version = await session.get(ReportVersion, report.current_version_id)
    return {
        "report": ReportView.model_validate(report).model_dump(mode="json"),
        "current_version": (
            {
                "id": str(version.id),
                "version": version.version,
                "title": version.title,
                "content": version.content,
                "review_status": version.review_status,
                "immutable": version.immutable,
            }
            if version
            else None
        ),
    }


@router.get("/reports/{report_id}/versions")
async def read_report_versions(
    report_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_READ))],
) -> list[dict]:
    report = await get_report(session, report_id)
    require_data_scope(
        user,
        report.organization_unit_id,
        Permissions.REPORT_READ,
        owner_id=report.created_by,
    )
    rows = await session.scalars(
        select(ReportVersion)
        .where(ReportVersion.report_id == report.id)
        .order_by(ReportVersion.version)
    )
    return [
        {
            "id": str(row.id),
            "version": row.version,
            "source": row.source,
            "review_status": row.review_status,
            "immutable": row.immutable,
        }
        for row in rows
    ]


@router.put("/reports/{report_id}/content")
async def put_report_content(
    report_id: UUID,
    payload: ReportContentUpdate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_EDIT))],
) -> dict:
    version = await update_report_content(
        session, report=await get_report(session, report_id), payload=payload, user=user
    )
    return {"version_id": str(version.id), "version": version.version}


@router.post("/reports/{report_id}/versions/{version_no}:restore")
async def post_restore_version(
    report_id: UUID,
    version_no: int,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_EDIT))],
) -> dict:
    version = await restore_report_version(
        session,
        report=await get_report(session, report_id),
        version_no=version_no,
        user=user,
    )
    return {"version_id": str(version.id), "version": version.version}


@router.post("/reports/{report_id}:submit-review")
async def post_submit_review(
    report_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_EDIT))],
) -> dict:
    report = await get_report(session, report_id)
    require_data_scope(
        user,
        report.organization_unit_id,
        Permissions.REPORT_EDIT,
        owner_id=report.created_by,
    )
    version = await session.get(ReportVersion, report.current_version_id)
    if version is None or version.review_status != "draft":
        raise ConflictError("only draft report version can be submitted")
    version.review_status = "pending_review"
    report.status = "pending_review"
    await session.commit()
    return {"id": str(report.id), "status": report.status}


@router.post("/reports/{report_id}:review")
async def post_review(
    report_id: UUID,
    payload: ReportReviewInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_REVIEW))],
) -> dict:
    report = await get_report(session, report_id)
    require_data_scope(
        user,
        report.organization_unit_id,
        Permissions.REPORT_REVIEW,
        owner_id=report.created_by,
    )
    version = await session.get(ReportVersion, report.current_version_id)
    if version is None or version.review_status != "pending_review":
        raise ConflictError("report version is not pending review")
    version.review_status = "approved" if payload.approved else "rejected"
    version.reviewed_by = user.user_id
    version.review_reason = payload.reason
    report.status = version.review_status
    await add_audit(
        session,
        user=user,
        action=f"report.{version.review_status}",
        resource_type="operation_report",
        resource_id=report.id,
        reason=payload.reason,
    )
    await session.commit()
    return {"id": str(report.id), "status": report.status}


@router.post("/reports/{report_id}:publish")
async def post_publish(
    report_id: UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_PUBLISH))],
) -> dict:
    report = await get_report(session, report_id)
    require_data_scope(
        user,
        report.organization_unit_id,
        Permissions.REPORT_PUBLISH,
        owner_id=report.created_by,
    )
    version = await session.get(ReportVersion, report.current_version_id)
    if version is None or version.review_status != "approved":
        raise ConflictError("only approved report version can be published")
    version.immutable = True
    report.published_version_id = version.id
    report.status = "published"
    await add_audit(
        session,
        user=user,
        action="report.publish",
        resource_type="operation_report",
        resource_id=report.id,
        after={"version": version.version},
    )
    await session.commit()
    return {"id": str(report.id), "status": report.status, "version": version.version}


@router.post("/reports/{report_id}:withdraw")
async def post_withdraw(
    report_id: UUID,
    payload: ReportWithdrawInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_WITHDRAW))],
) -> dict:
    report = await get_report(session, report_id)
    require_data_scope(
        user,
        report.organization_unit_id,
        Permissions.REPORT_WITHDRAW,
        owner_id=report.created_by,
    )
    if report.status != "published":
        raise ConflictError("only published report can be withdrawn")
    report.status = "withdrawn"
    await add_audit(
        session,
        user=user,
        action="report.withdraw",
        resource_type="operation_report",
        resource_id=report.id,
        reason=payload.reason,
    )
    await session.commit()
    return {"id": str(report.id), "status": report.status}


@router.post("/reports/{report_id}/exports", status_code=202)
async def post_export(
    report_id: UUID,
    payload: ReportExportCreate,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_EXPORT))],
) -> dict:
    export = await create_export(
        session,
        report=await get_report(session, report_id),
        export_format=payload.format,
        user=user,
        storage=request.app.state.providers.storage,
        inline=request.app.state.settings.run_tasks_inline,
    )
    return {"export_id": str(export.id), "status": export.status}


@router.get("/reports/{report_id}/exports/{export_id}")
async def read_export(
    report_id: UUID,
    export_id: UUID,
    request: Request,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.REPORT_EXPORT))],
) -> dict:
    report = await get_report(session, report_id)
    require_data_scope(
        user,
        report.organization_unit_id,
        Permissions.REPORT_EXPORT,
        owner_id=report.created_by,
    )
    export = await session.get(ReportExport, export_id)
    if export is None or export.report_id != report.id:
        raise NotFoundError("report_export", export_id)
    download_url = None
    if export.status == "succeeded" and export.object_key:
        download_url = await request.app.state.providers.storage.signed_download_url(
            export.object_key
        )
    return {"export_id": str(export.id), "status": export.status, "download_url": download_url}

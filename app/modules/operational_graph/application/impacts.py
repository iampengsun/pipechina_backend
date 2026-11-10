"""Version-change impact paths and independently recorded human review decisions."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.modules.maintenance_order.domain.models import WorkOrderAcceptance, WorkOrderResolution
from app.modules.operation_event.domain.memory_models import ExperienceMemory
from app.modules.operational_graph.application.catalog import (
    build_catalog,
    digest,
    key,
    record_revision,
)
from app.modules.operational_graph.application.queries import checked_paths, validation_snapshot
from app.modules.operational_graph.domain.models import ImpactReview
from app.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.platform.service import add_audit
from app.shared.security.authorization.permissions import Permissions as P


def change_details(catalog, version):
    newer = [
        r.row
        for r in catalog.records.values()
        if r.node.kind == "knowledge_version"
        and r.row.document_id == version.document_id
        and r.row.number > version.number
        and r.state == "published"
        and r.row.effective_from <= datetime.now(UTC)
        and (r.row.effective_to is None or r.row.effective_to > datetime.now(UTC))
    ]
    newer.sort(key=lambda r: r.number, reverse=True)
    replacement = newer[0] if newer else None
    kind = (
        "withdrawal"
        if version.status == "withdrawn"
        else "expiry"
        if version.effective_to and version.effective_to <= datetime.now(UTC)
        else "new_revision"
        if replacement
        else None
    )
    if not kind:
        return None
    signature = digest(
        [
            version.id,
            kind,
            version.status,
            version.effective_to,
            replacement.id if replacement else None,
        ]
    )
    return {
        "change_type": kind,
        "change_signature": signature,
        "replacement_version_id": replacement.id if replacement else None,
    }


async def impact(session, user, graph, version_id):
    catalog, paths, state = await checked_paths(
        session,
        user,
        graph,
        key("knowledge_version", version_id),
        depth=6,
        limit=1000,
        dependencies_only=True,
    )
    version = catalog.records[key("knowledge_version", version_id)].row
    targets: dict[str, dict] = {}
    for path in paths:
        record = catalog.records[path.nodes[-1]]
        if (record.node.kind == "work_order" and record.state not in {"closed", "cancelled"}) or (
            record.node.kind == "experience"
            and record.state == "approved"
            and record.row.expires_at > datetime.now(UTC)
        ):
            targets.setdefault(
                record.node.key, {"record": catalog.view(record.node.key), "paths": []}
            )["paths"].append(path.model_dump())
    return {
        "source": catalog.view(key("knowledge_version", version_id)),
        "change": change_details(catalog, version),
        "targets": list(targets.values()),
        "projection": state,
        "validation": validation_snapshot(catalog, paths, key("knowledge_version", version_id)),
        "interpretation": "新版本发布提示复核适用性，不自动认定旧版本失效；历史业务记录保持原状。",
    }


async def materialize_reviews(session, catalog, graph, generation):
    # This system operation stores IDs only. Reading or deciding any case repeats source ACL checks.
    allowed = list(catalog.records)
    count = 0
    signatures = {}
    for record in catalog.records.values():
        if record.node.kind != "knowledge_version":
            continue
        change = change_details(catalog, record.row)
        if not change:
            continue
        signatures[record.row.id] = change["change_signature"]
        paths = await graph.paths(
            generation, record.node.key, allowed, 6, 1001, dependencies_only=True
        )
        if len(paths) > 1000:
            raise ConflictError(
                "impact path capacity exceeded; source review cannot be declared complete"
            )
        grouped: dict[str, list[dict]] = {}
        for path in paths:
            if len(set(path.nodes)) != len(path.nodes):
                continue
            target = catalog.records[path.nodes[-1]]
            if (
                target.node.kind == "work_order" and target.state not in {"closed", "cancelled"}
            ) or (
                target.node.kind == "experience"
                and target.state == "approved"
                and target.row.expires_at > datetime.now(UTC)
            ):
                grouped.setdefault(target.node.key, []).append(path.model_dump())
        for target, paths_json in grouped.items():
            paths_json = sorted(paths_json, key=lambda p: tuple(p["edges"]))
            existing = await session.scalar(
                select(ImpactReview).where(
                    ImpactReview.source_version_id == record.row.id,
                    ImpactReview.target_key == target,
                    ImpactReview.change_signature == change["change_signature"],
                )
            )
            if existing:
                revision = catalog.records[target].node.revision
                if existing.status == "closed" and existing.target_revision != revision:
                    existing.status = "pending"
                    existing.version += 1
                    existing.history = [
                        *existing.history,
                        {"action": "target_changed", "at": datetime.now(UTC).isoformat()},
                    ]
                    existing.target_revision = revision
                if existing.status in {"pending", "needs_action"} and existing.paths != paths_json:
                    existing.paths = paths_json
                    existing.version += 1
                continue
            session.add(
                ImpactReview(
                    source_version_id=record.row.id,
                    target_key=target,
                    target_revision=catalog.records[target].node.revision,
                    paths=paths_json,
                    history=[{"action": "detected", "at": datetime.now(UTC).isoformat()}],
                    **change,
                )
            )
            count += 1
    for review in await session.scalars(
        select(ImpactReview).where(ImpactReview.status.in_(["pending", "needs_action"]))
    ):
        if signatures.get(review.source_version_id) != review.change_signature:
            review.status = "superseded"
            review.version += 1
            review.history = [
                *review.history,
                {"action": "source_changed", "at": datetime.now(UTC).isoformat()},
            ]
    return count


def review_access(catalog, user, row):
    if not user.has_permission(P.GRAPH_REVIEW):
        raise PermissionDeniedError(P.GRAPH_REVIEW)
    required = {key("knowledge_version", row.source_version_id), row.target_key}
    for path in row.paths:
        required.update(path["nodes"])
    if any(not catalog.can_read(k, user) for k in required):
        raise PermissionDeniedError(P.GRAPH_REVIEW)


async def read_review(session, user, review_id):
    row = await session.get(ImpactReview, review_id, populate_existing=True)
    if row is None:
        raise NotFoundError("impact_review", review_id)
    catalog = await build_catalog(session)
    review_access(catalog, user, row)
    return view_review(catalog, row)


def view_review(catalog, row):
    return {
        "id": row.id,
        "source_version_id": row.source_version_id,
        "replacement_version_id": row.replacement_version_id,
        "change_type": row.change_type,
        "change_signature": row.change_signature,
        "target_key": row.target_key,
        "target_revision": catalog.records[row.target_key].node.revision,
        "status": row.status,
        "version": row.version,
        "paths": row.paths,
        "paths_current": all(e in catalog.edges for p in row.paths for e in p["edges"]),
        "history": row.history,
        "reason": row.reason,
        "reviewed_by": row.reviewed_by,
        "created_at": row.created_at,
        "reviewed_at": row.reviewed_at,
    }


async def decide_review(session, user, review_id, payload):
    row = await session.get(ImpactReview, review_id, with_for_update=True, populate_existing=True)
    if row is None:
        raise NotFoundError("impact_review", review_id)
    catalog = await build_catalog(session)
    review_access(catalog, user, row)
    change = change_details(
        catalog, catalog.records[key("knowledge_version", row.source_version_id)].row
    )
    if (
        row.version != payload.expected_version
        or row.status not in {"pending", "needs_action"}
        or not change
        or change["change_signature"] != row.change_signature
    ):
        raise ConflictError("impact review or source version changed")
    target = catalog.records[row.target_key]
    await session.refresh(target.row, with_for_update=True)
    if record_revision(target.row, target.row.status) != payload.target_revision:
        raise ConflictError("target business record changed")
    if target.node.revision != payload.target_revision:
        raise ConflictError("target business record changed")
    from app.shared.security.authorization.dependencies import require_data_scope

    require_data_scope(
        user,
        target.organization,
        P.MAINTENANCE_REVIEW,
        owner_id=target.owner,
        assignee_id=target.assignee,
    )
    if payload.decision == "revalidated":
        if row.status != "needs_action" or not payload.verification_id:
            raise ConflictError("revalidation requires an action and a fresh verification record")
        if target.node.kind == "work_order":
            verification = await session.get(WorkOrderAcceptance, payload.verification_id)
            if (
                verification is None
                or str(verification.work_order_id) != target.node.record_id
                or not verification.approved
                or verification.created_at < row.created_at
                or target.state != "closed"
            ):
                raise ConflictError(
                    "revalidation needs this order’s independent acceptance after the change review"
                )
            latest = await session.scalar(
                select(WorkOrderResolution)
                .where(WorkOrderResolution.work_order_id == target.row.id)
                .order_by(WorkOrderResolution.order_version.desc())
                .limit(1)
            )
            if (
                latest is None
                or verification.resolution_id != latest.id
                or not latest.measurements
                or not verification.measurements
                or verification.reviewed_by in {latest.submitted_by, target.assignee}
            ):
                raise ConflictError(
                    "revalidation requires the latest measured resolution "
                    "and independent acceptance"
                )
        else:
            successor = await session.get(ExperienceMemory, payload.verification_id)
            if (
                successor is None
                or successor.status != "approved"
                or successor.expires_at <= datetime.now(UTC)
                or target.node.record_id not in [str(x) for x in successor.supersedes_ids]
                or successor.created_at < row.created_at
                or not catalog.can_read(key("experience", successor.id), user)
            ):
                raise ConflictError(
                    "experience revalidation needs an "
                    "approved replacement linked to this experience"
                )
        row.status = "closed"
    elif payload.decision == "retired":
        if (
            target.state not in {"cancelled", "withdrawn", "superseded"}
            or target.row.updated_at < row.created_at
        ):
            raise ConflictError(
                "retirement requires a source withdrawal or cancellation after detection"
            )
        row.status = "closed"
    elif payload.decision == "not_applicable":
        if row.status != "pending":
            raise ConflictError("an action-required review needs verification before closure")
        row.status = "closed"
    else:
        row.status = "needs_action"
    row.target_revision = target.node.revision
    row.reviewed_by = user.user_id
    row.reviewed_at = datetime.now(UTC)
    row.reason = payload.reason
    row.version += 1
    row.history = [
        *row.history,
        {
            "action": payload.decision,
            "actor_id": str(user.user_id),
            "at": row.reviewed_at.isoformat(),
            "reason": payload.reason,
            "verification_id": str(payload.verification_id) if payload.verification_id else None,
            "target_revision": payload.target_revision,
        },
    ]
    await add_audit(
        session,
        user=user,
        action="graph.impact.review",
        resource_type="impact_review",
        resource_id=row.id,
        reason=payload.reason,
    )
    await session.commit()
    await session.refresh(row)
    return view_review(catalog, row)

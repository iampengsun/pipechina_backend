"""Compare confirmed episodes for an exact equipment/failure identity, with measured history."""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.modules.hazard.domain.models import Hazard, HazardWorkOrderLink
from app.modules.maintenance_order.domain.models import WorkOrderAcceptance, WorkOrderResolution
from app.modules.operation_event.domain.models import ProductionEvent
from app.modules.operational_graph.application.assets import asset_access, equipment_access
from app.modules.operational_graph.application.catalog import binding_revision, key
from app.modules.operational_graph.application.queries import checked_paths, validation_snapshot
from app.modules.operational_graph.domain.models import EquipmentAssociation, FailureMode
from app.shared.errors import ConflictError, NotFoundError


async def recurrence(
    session, user, graph, equipment_id, failure_mode_id, *, since=None, until=None
):
    equipment = await equipment_access(session, user, equipment_id)
    failure = await session.get(FailureMode, failure_mode_id)
    if failure is None:
        raise NotFoundError("failure_mode", failure_mode_id)
    asset_access(failure, user)
    if failure.organization_unit_id != equipment.organization_unit_id:
        raise ConflictError("failure mode belongs to another organization")
    if since and until and since >= until:
        raise ConflictError("recurrence time range is invalid")
    catalog, paths, state = await checked_paths(
        session, user, graph, key("equipment", equipment_id), depth=6, limit=1000
    )
    reached = {identifier for path in paths for identifier in path.nodes}
    links = list(
        await session.scalars(
            select(EquipmentAssociation).where(
                EquipmentAssociation.equipment_id == equipment_id,
                EquipmentAssociation.failure_mode_id == failure_mode_id,
                EquipmentAssociation.active.is_(True),
            )
        )
    )
    confirmed_orders = set()
    candidates = set()
    stale = False
    hazard_links = list(await session.scalars(select(HazardWorkOrderLink)))
    for link in links:
        identifier = key(link.record_kind, link.record_id)
        if identifier not in catalog.records or not catalog.can_read(identifier, user):
            continue
        record = catalog.records[identifier]
        if binding_revision(record.row) != link.source_revision:
            stale = True
            continue
        if identifier not in reached:
            continue
        if link.record_kind == "work_order":
            confirmed_orders.add(link.record_id)
            order = record.row
            if order.event_id:
                candidates.add(key("event", order.event_id))
            candidates.update(
                key("hazard", h.hazard_id) for h in hazard_links if h.work_order_id == order.id
            )
        else:
            candidates.add(identifier)
    groups: dict[str, dict[str, Any]] = {}
    event_id: UUID | None
    for identifier in sorted(candidates):
        if (
            identifier not in reached
            or identifier not in catalog.records
            or not catalog.can_read(identifier, user)
        ):
            continue
        record = catalog.records[identifier]
        row = record.row
        if isinstance(row, ProductionEvent):
            if row.business_status != "confirmed":
                continue
            observed = row.occurred_at or row.created_at
            basis = "occurred_at" if row.occurred_at else "registered_at"
            episode_key = identifier
            event_id = row.id
        elif isinstance(row, Hazard):
            if row.risk_level is None or row.status == "cancelled":
                continue
            event_id = row.source_id if row.source_type == "production_event" else None
            event_record = catalog.records.get(key("event", event_id))
            if (
                event_record
                and catalog.can_read(event_record.node.key, user)
                and event_record.state == "confirmed"
            ):
                observed = event_record.row.occurred_at or event_record.row.created_at
                basis = "occurred_at" if event_record.row.occurred_at else "registered_at"
                episode_key = event_record.node.key
            else:
                event_id = None
                observed = row.created_at
                basis = "registered_at"
                episode_key = identifier
        else:
            continue
        if (since and observed < since) or (until and observed >= until):
            continue
        episode = groups.setdefault(
            episode_key,
            {
                "key": episode_key,
                "event_id": event_id,
                "observed_at": observed,
                "time_basis": basis,
                "records": [],
                "orders": [],
            },
        )
        episode["records"].append(catalog.view(identifier))
    resolutions = list(await session.scalars(select(WorkOrderResolution)))
    acceptances = {r.resolution_id: r for r in await session.scalars(select(WorkOrderAcceptance))}
    for episode in groups.values():
        hazards = {r["record_id"] for r in episode["records"] if r["kind"] == "hazard"}
        episode["hazard_outcomes"] = []
        for hazard_id in sorted(hazards):
            hazard = catalog.records[key("hazard", hazard_id)].row
            history = sorted(
                [
                    r
                    for r in catalog.records.values()
                    if r.node.kind == "hazard_rectification"
                    and str(r.row.hazard_id) == hazard_id
                    and r.node.key in reached
                    and catalog.can_read(r.node.key, user)
                ],
                key=lambda r: (r.row.submitted_at, str(r.row.id)),
            )
            latest = history[-1].row if history else None
            accepted = next(
                (
                    r.row
                    for r in catalog.records.values()
                    if r.node.kind == "hazard_acceptance"
                    and latest
                    and r.row.rectification_id == latest.id
                    and r.node.key in reached
                    and catalog.can_read(r.node.key, user)
                ),
                None,
            )
            measured = bool(
                hazard.status == "closed"
                and latest
                and latest.measurements
                and accepted
                and accepted.measurements
                and accepted.decision == "accepted"
                and accepted.accepted_by not in {latest.submitted_by, hazard.assignee_id}
            )
            episode["hazard_outcomes"].append(
                {
                    "id": hazard_id,
                    "state": hazard.status,
                    "measured_accepted": measured,
                    "action": latest.action if latest else None,
                    "result": latest.result if latest else None,
                    "measurements": latest.measurements if latest else [],
                    "verification_measurements": accepted.measurements
                    if measured and accepted
                    else [],
                    "acceptance_id": str(accepted.id) if measured and accepted else None,
                    "accepted_at": accepted.accepted_at if measured and accepted else None,
                }
            )
        for record in catalog.records.values():
            if (
                record.node.kind != "work_order"
                or record.node.key not in reached
                or not catalog.can_read(record.node.key, user)
            ):
                continue
            order = record.row
            if not (
                (episode["event_id"] and order.event_id == episode["event_id"])
                or any(
                    str(link.hazard_id) in hazards and link.work_order_id == order.id
                    for link in hazard_links
                )
            ):
                continue
            history = sorted(
                [r for r in resolutions if r.work_order_id == order.id],
                key=lambda r: r.order_version,
            )
            latest = history[-1] if history else None
            acceptance = acceptances.get(latest.id) if latest else None
            measured = bool(
                order.id in confirmed_orders
                and order.status == "closed"
                and latest
                and acceptance
                and acceptance.approved
                and latest.measurements
                and acceptance.reviewed_by not in {latest.submitted_by, order.assignee_id}
            )
            episode["orders"].append(
                {
                    "id": str(order.id),
                    "state": order.status,
                    "confirmed_for_equipment": order.id in confirmed_orders,
                    "measured_accepted": measured,
                    "action": latest.action if latest else None,
                    "result": latest.result if latest else None,
                    "measurements": latest.measurements if latest else [],
                    "verification_measurements": acceptance.measurements
                    if measured and acceptance
                    else [],
                    "acceptance_id": str(acceptance.id) if measured and acceptance else None,
                    "accepted_at": acceptance.created_at if measured and acceptance else None,
                }
            )
    episodes = sorted(groups.values(), key=lambda e: (e["observed_at"], e["key"]))
    for episode in episodes:
        for order in [*episode["orders"], *episode["hazard_outcomes"]]:
            later = next(
                (
                    e
                    for e in episodes
                    if order["accepted_at"]
                    and e["key"] != episode["key"]
                    and e["observed_at"] > order["accepted_at"]
                ),
                None,
            )
            order["next_record_at"] = later["observed_at"] if later else None
            order["days_to_next_record"] = (
                round((later["observed_at"] - order["accepted_at"]).total_seconds() / 86400, 3)
                if later
                else None
            )
            order["next_record_time_basis"] = later["time_basis"] if later else None
        episode.pop("event_id")
    return {
        "equipment_id": equipment.id,
        "failure_mode_id": failure.id,
        "episodes": episodes,
        "visible_episode_count": len(episodes),
        "repeated_observations": len(episodes) >= 2,
        "stale_associations": stale,
        "projection": state,
        "validation": validation_snapshot(catalog, paths, key("equipment", equipment_id)),
        "interpretation": (
            "仅统计当前权限内、人工确认设备及故障类型的正式事件/已评估隐患；"
            "同一事件派生的隐患合并计数。再次登记间隔不代表措施无效或因果关系。"
        ),
    }

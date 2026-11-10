"""Event-scoped graph tools shared by the investigation agent and authenticated MCP."""

from uuid import UUID

from sqlalchemy import select

from app.modules.operation_event.application.memory import event_access
from app.modules.operational_graph.application.catalog import binding_revision, build_catalog, key
from app.modules.operational_graph.application.impacts import impact
from app.modules.operational_graph.application.queries import graph_client, trace
from app.modules.operational_graph.application.recurrence import recurrence
from app.modules.operational_graph.domain.models import EquipmentAssociation
from app.modules.operational_graph.domain.schemas import ImpactView, RecurrenceView, TraceView
from app.shared.errors import ConflictError


async def investigate_graph(session, user, manager, event_id, operation):
    event = await event_access(session, user, event_id)
    graph = graph_client(manager)
    context = await trace(session, user, graph, "event", event.id, depth=6, limit=1000)
    catalog = await build_catalog(session)
    related = {key("event", event.id)}
    for identifier, record in catalog.records.items():
        if record.node.kind in {"work_order", "agent_run"} and record.row.event_id == event.id:
            related.add(identifier)
        if (
            record.node.kind == "hazard"
            and record.row.source_type == "production_event"
            and record.row.source_id == event.id
        ):
            related.add(identifier)
    results = []
    if operation == "trace_case_relations":
        results = [TraceView.model_validate(context).model_dump(mode="json")]
    elif operation == "find_recurrent_hazards":
        source_keys = {n["key"] for n in context["nodes"]} & related
        pairs = set()
        for link in await session.scalars(
            select(EquipmentAssociation).where(EquipmentAssociation.active.is_(True))
        ):
            identifier = key(link.record_kind, link.record_id)
            if (
                identifier in source_keys
                and binding_revision(catalog.records[identifier].row) == link.source_revision
            ):
                pairs.add((link.equipment_id, link.failure_mode_id))
        if len(pairs) > 5:
            raise ConflictError(
                "event has more equipment/failure groups than a single "
                "investigation can inspect; use equipment recurrence API"
            )
        for equipment_id, failure_id in sorted(pairs, key=str):
            value = await recurrence(session, user, graph, equipment_id, failure_id)
            results.append(RecurrenceView.model_validate(value).model_dump(mode="json"))
    elif operation == "assess_change_impact":
        visible = {n["key"] for n in context["nodes"]}
        versions = {
            catalog.records[e.source].node.record_id
            for e in catalog.edges.values()
            if e.relation == "CITED_BY"
            and e.target in related
            and e.source in visible
            and catalog.records[e.source].node.kind == "knowledge_version"
        }
        if len(versions) > 5:
            raise ConflictError(
                "event cites more versions than a single "
                "investigation can inspect; use version impact API"
            )
        for version_id in sorted(versions):
            value = await impact(session, user, graph, UUID(version_id))
            results.append(ImpactView.model_validate(value).model_dump(mode="json"))
    else:
        raise ConflictError("unknown business graph tool")
    if not results:
        results = [
            {
                "observations": [],
                "unknowns": [
                    "当前可见证据中未发现人工确认的设备故障关联或知识引用；不能据此认定不存在复发或变更影响。"
                ],
                "projection": context["projection"],
                "validation": context["validation"],
            }
        ]
    for result in results:
        result["record_id"] = str(event.id)
        result["version_id"] = str(event.current_version_id)
    return results

"""Construct relationships from stored IDs, never from name similarity or model guesses."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.modules.handover.domain.models import AudioRecord, ManualOperationRecord
from app.modules.hazard.domain.models import (
    Hazard,
    HazardAcceptance,
    HazardRectification,
    HazardWorkOrderLink,
)
from app.modules.inspection.domain.models import InspectionFinding, InspectionRecord
from app.modules.knowledge.application.access import authorize
from app.modules.knowledge.domain.models import (
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeRun,
    KnowledgeVersion,
)
from app.modules.maintenance_order.domain.models import (
    WorkOrder,
    WorkOrderAcceptance,
    WorkOrderResolution,
)
from app.modules.meeting.domain.models import Meeting, MeetingKnowledgePublication
from app.modules.operation_event.domain.agent_models import AgentEvidence, AgentRun
from app.modules.operation_event.domain.memory_models import ExperienceMemory
from app.modules.operation_event.domain.models import EventSourceLink, ProductionEvent
from app.modules.operational_graph.domain.models import (
    Equipment,
    EquipmentAssociation,
    FailureMode,
    Station,
)
from app.ports.business_graph import BusinessEdge, BusinessNode
from app.shared.errors import ConflictError, PermissionDeniedError
from app.shared.platform.models import WorkflowRun
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions as P
from app.shared.security.identity.models import UserAccount

DEPENDENCIES = {
    "CITED_BY",
    "PROPOSED_ORDER",
    "FOLLOWED_BY",
    "CONSOLIDATED_IN",
    "SUPPORTS_EXPERIENCE",
    "USED_IN",
}
MAX_RECORDS = 50000


def key(kind, identity):
    return f"{kind}:{identity}"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def binding_revision(row):
    if isinstance(row, ProductionEvent):
        return digest([row.current_version_id])
    if isinstance(row, Hazard):
        return digest(
            [row.title, row.description, row.location, row.category, row.source_type, row.source_id]
        )
    if isinstance(row, WorkOrder):
        return digest([row.title, row.description, row.event_id])
    raise ValueError("unsupported equipment association source")


def record_revision(row, state, details=None):
    if isinstance(row, ProductionEvent):
        return digest([row.current_version_id, row.title, state])
    return digest([row.updated_at, state, details])


@dataclass
class Record:
    node: BusinessNode
    row: Any
    title: str
    state: str
    organization: UUID
    owner: UUID
    permission: str
    assignee: UUID | None = None
    details: dict = field(default_factory=dict)


class Catalog:
    def __init__(self):
        self.records: dict[str, Record] = {}
        self.edges: dict[str, BusinessEdge] = {}
        self.bases = {}
        self.documents = {}
        self.publications = []
        self.allowed_cache: dict[str, bool] = {}

    def add(self, kind, row, title, state, org, owner, permission, *, assignee=None, details=None):
        identifier = key(kind, row.id)
        self.records[identifier] = Record(
            BusinessNode(
                key=identifier,
                kind=kind,
                record_id=str(row.id),
                revision=record_revision(row, state, details),
            ),
            row,
            title,
            state,
            org,
            owner,
            permission,
            assignee,
            details or {},
        )
        if len(self.records) > MAX_RECORDS:
            raise ConflictError(
                "business graph exceeds bounded snapshot capacity", limit=MAX_RECORDS
            )

    def link(self, source, target, relation, reference):
        if source not in self.records or target not in self.records:
            return
        identifier = digest([source, target, relation, str(reference)])
        self.edges[identifier] = BusinessEdge(
            key=identifier,
            source=source,
            target=target,
            relation=relation,
            reference=str(reference),
        )

    def can_read(self, identifier, user, visiting=None):
        if identifier not in self.records:
            return False
        if identifier in self.allowed_cache:
            return self.allowed_cache[identifier]
        visiting = set() if visiting is None else set(visiting)
        if identifier in visiting:
            return False
        visiting.add(identifier)
        record = self.records[identifier]
        row = record.row
        try:
            require_data_scope(
                user,
                record.organization,
                record.permission,
                owner_id=record.owner,
                assignee_id=record.assignee,
            )
            if (
                isinstance(row, Meeting)
                and row.reader_ids
                and str(user.user_id) not in row.reader_ids
                and user.user_id != row.created_by
                and not user.has_global_permission(P.ALL)
            ):
                raise PermissionDeniedError(P.MEETING_READ)
            if isinstance(row, KnowledgeDocument | KnowledgeVersion):
                document = (
                    row if isinstance(row, KnowledgeDocument) else self.documents[row.document_id]
                )
                base = self.bases[document.base_id]
                authorize(user, base, document, P.KNOWLEDGE_READ)
                for publication in self.publications:
                    if publication.knowledge_document_id == document.id:
                        meeting = self.records[key("meeting", publication.meeting_id)].row
                        if (
                            meeting.reader_ids
                            and str(user.user_id) not in meeting.reader_ids
                            and user.user_id != meeting.created_by
                            and not user.has_global_permission(P.ALL)
                        ):
                            raise PermissionDeniedError(P.KNOWLEDGE_READ)
                if (
                    isinstance(row, KnowledgeVersion)
                    and row.status != "published"
                    and not user.has_global_permission(P.ALL)
                ):
                    for editorial in [P.KNOWLEDGE_EDIT, P.KNOWLEDGE_REVIEW, P.KNOWLEDGE_INDEX]:
                        try:
                            authorize(user, base, document, editorial)
                            break
                        except PermissionDeniedError:
                            continue
                    else:
                        raise PermissionDeniedError(P.KNOWLEDGE_READ)
            if (
                isinstance(row, KnowledgeRun)
                and row.requested_by != user.user_id
                and not user.has_global_permission(P.ALL)
            ):
                raise PermissionDeniedError(P.KNOWLEDGE_READ)
            if isinstance(row, ExperienceMemory):
                if not user.has_permission(P.GRAPH_REVIEW) and (
                    row.status != "approved" or row.expires_at <= datetime.now(UTC)
                ):
                    raise PermissionDeniedError(P.MAINTENANCE_READ)
                for source in row.source_orders:
                    if not self.can_read(key("work_order", source["id"]), user, visiting):
                        raise PermissionDeniedError(P.MAINTENANCE_READ)
                ancestors = {identifier}
                for _ in range(6):
                    ancestors |= {
                        e.source
                        for e in self.edges.values()
                        if e.target in ancestors and e.relation in DEPENDENCIES
                    }
                for parent in ancestors - {identifier}:
                    ancestor = self.records[parent]
                    if ancestor.node.kind == "knowledge_version" and not user.has_permission(
                        P.GRAPH_REVIEW
                    ):
                        v = ancestor.row
                        if (
                            v.status != "published"
                            or v.effective_from > datetime.now(UTC)
                            or (v.effective_to and v.effective_to <= datetime.now(UTC))
                        ):
                            raise PermissionDeniedError(P.KNOWLEDGE_READ)
                    if self.records[parent].node.kind in {
                        "knowledge_version",
                        "experience",
                    } and not self.can_read(parent, user, visiting):
                        raise PermissionDeniedError(P.MAINTENANCE_READ)
            self.allowed_cache[identifier] = True
        except PermissionDeniedError, KeyError:
            self.allowed_cache[identifier] = False
        return self.allowed_cache[identifier]

    def view(self, identifier):
        r = self.records[identifier]
        return {
            **r.node.model_dump(),
            "title": r.title,
            "state": r.state,
            "binding_revision": binding_revision(r.row)
            if isinstance(r.row, (ProductionEvent, Hazard, WorkOrder))
            else None,
            "details": r.details,
        }


async def build_catalog(session):
    c = Catalog()

    async def rows(model):
        values = list(
            await session.scalars(
                select(model).limit(MAX_RECORDS + 1).execution_options(populate_existing=True)
            )
        )
        if len(values) > MAX_RECORDS:
            raise ConflictError(
                "business graph source capacity exceeded", source=model.__tablename__
            )
        return values

    for r in await rows(Station):
        c.add(
            "station", r, r.name, "active", r.organization_unit_id, r.created_by, P.EQUIPMENT_READ
        )
    for r in await rows(Equipment):
        c.add(
            "equipment",
            r,
            r.name,
            "active" if r.active else "inactive",
            r.organization_unit_id,
            r.created_by,
            P.EQUIPMENT_READ,
        )
        c.link(key("station", r.station_id), key("equipment", r.id), "HAS_EQUIPMENT", r.id)
    for r in await rows(FailureMode):
        c.add(
            "failure_mode",
            r,
            r.name,
            "active",
            r.organization_unit_id,
            r.created_by,
            P.EQUIPMENT_READ,
        )
    for r in await rows(ProductionEvent):
        c.add(
            "event",
            r,
            r.title,
            r.business_status,
            r.organization_unit_id,
            r.created_by,
            P.EVENT_READ,
        )
    for r in await rows(Hazard):
        c.add(
            "hazard",
            r,
            r.title,
            r.status,
            r.organization_unit_id,
            r.created_by,
            P.HAZARD_READ,
            assignee=r.assignee_id,
        )
    for r in await rows(Meeting):
        c.add("meeting", r, r.title, r.status, r.organization_unit_id, r.created_by, P.MEETING_READ)
    for r in await rows(AudioRecord):
        c.add(
            "audio",
            r,
            r.filename,
            r.business_status,
            r.organization_unit_id,
            r.created_by,
            P.HANDOVER_READ,
        )
    for r in await rows(ManualOperationRecord):
        c.add(
            "manual_record",
            r,
            r.record_type,
            "recorded",
            r.organization_unit_id,
            r.created_by,
            P.HANDOVER_READ,
        )
    for r in await rows(InspectionRecord):
        c.add(
            "inspection",
            r,
            r.station_name,
            r.status,
            r.organization_unit_id,
            r.created_by,
            P.INSPECTION_READ,
        )
    for r in await rows(InspectionFinding):
        parent = c.records.get(key("inspection", r.inspection_id))
        if parent:
            c.add(
                "inspection_finding",
                r,
                r.title,
                r.review_status,
                parent.organization,
                parent.owner,
                P.INSPECTION_READ,
            )
            c.link(parent.node.key, key("inspection_finding", r.id), "FOUND", r.id)
    for r in await rows(AgentRun):
        if r.status not in {"succeeded", "failed", "cancelled"}:
            continue
        c.add(
            "agent_run", r, "调查运行", r.status, r.organization_unit_id, r.created_by, P.EVENT_READ
        )
        c.link(
            key("event", r.event_id), key("agent_run", r.id), "INVESTIGATED_BY", r.event_version_id
        )
    workflows = {r.id: r for r in await rows(WorkflowRun)}
    for r in await rows(WorkOrder):
        c.add(
            "work_order",
            r,
            r.title,
            r.status,
            r.organization_unit_id,
            r.created_by,
            P.MAINTENANCE_READ,
            assignee=r.assignee_id,
        )
        c.link(key("event", r.event_id), key("work_order", r.id), "FOLLOWED_BY", r.id)
        workflow = workflows.get(r.source_workflow_run_id)
        if workflow and workflow.state_snapshot.get("agent_run_id"):
            c.link(
                key("agent_run", workflow.state_snapshot["agent_run_id"]),
                key("work_order", r.id),
                "PROPOSED_ORDER",
                workflow.id,
            )
    for r in await rows(WorkOrderResolution):
        parent = c.records.get(key("work_order", r.work_order_id))
        if parent:
            c.add(
                "resolution",
                r,
                "整改复测",
                "recorded",
                parent.organization,
                parent.owner,
                P.MAINTENANCE_READ,
                assignee=parent.assignee,
                details={
                    "action": r.action,
                    "result": r.result,
                    "measurements": r.measurements,
                    "order_version": r.order_version,
                },
            )
            c.link(parent.node.key, key("resolution", r.id), "RESOLVED_WITH", r.id)
    for r in await rows(WorkOrderAcceptance):
        parent = c.records.get(key("work_order", r.work_order_id))
        if parent:
            c.add(
                "acceptance",
                r,
                "独立验收",
                "approved" if r.approved else "rejected",
                parent.organization,
                parent.owner,
                P.MAINTENANCE_READ,
                assignee=parent.assignee,
                details={
                    "measurements": r.measurements,
                    "reason": r.reason,
                    "reviewed_by": str(r.reviewed_by),
                },
            )
            c.link(key("resolution", r.resolution_id), key("acceptance", r.id), "VERIFIED_BY", r.id)
    for kind, model in [
        ("hazard_rectification", HazardRectification),
        ("hazard_acceptance", HazardAcceptance),
    ]:
        for r in await rows(model):
            parent = c.records.get(key("hazard", r.hazard_id))
            if parent:
                c.add(
                    kind,
                    r,
                    "隐患整改" if kind == "hazard_rectification" else "隐患验收",
                    r.decision if isinstance(r, HazardAcceptance) else "recorded",
                    parent.organization,
                    parent.owner,
                    P.HAZARD_READ,
                    assignee=parent.assignee,
                    details=(
                        {"measurements": r.measurements, "action": r.action, "result": r.result}
                        if isinstance(r, HazardRectification)
                        else {
                            "measurements": r.measurements,
                            "reason": r.reason,
                            "rectification_id": str(r.rectification_id),
                            "accepted_by": str(r.accepted_by),
                        }
                    ),
                )
                c.link(
                    parent.node.key,
                    key(kind, r.id),
                    "RECTIFICATION" if kind == "hazard_rectification" else "ACCEPTANCE",
                    r.id,
                )
    for r in await rows(ExperienceMemory):
        c.add(
            "experience",
            r,
            "经验记录",
            r.status,
            r.organization_unit_id,
            r.created_by,
            P.MAINTENANCE_READ,
        )
        for source in r.source_orders:
            c.link(
                key("work_order", source["id"]), key("experience", r.id), "CONSOLIDATED_IN", r.id
            )
    for r in [record.row for record in c.records.values() if record.node.kind == "experience"]:
        for parent in r.dependency_ids:
            c.link(key("experience", parent), key("experience", r.id), "SUPPORTS_EXPERIENCE", r.id)
    c.bases = {r.id: r for r in await rows(KnowledgeBase)}
    c.documents = {r.id: r for r in await rows(KnowledgeDocument)}
    for r in c.documents.values():
        base = c.bases[r.base_id]
        c.add(
            "knowledge_document",
            r,
            r.title,
            "recorded",
            base.organization_unit_id,
            r.created_by,
            P.KNOWLEDGE_READ,
        )
    for r in await rows(KnowledgeVersion):
        parent = c.records.get(key("knowledge_document", r.document_id))
        if parent:
            c.add(
                "knowledge_version",
                r,
                r.filename,
                r.status,
                parent.organization,
                parent.owner,
                P.KNOWLEDGE_READ,
                details={
                    "version_number": r.number,
                    "effective_from": r.effective_from.isoformat(),
                    "effective_to": r.effective_to.isoformat() if r.effective_to else None,
                    "currently_effective": r.status == "published"
                    and r.effective_from <= datetime.now(UTC)
                    and (r.effective_to is None or r.effective_to > datetime.now(UTC)),
                },
            )
            c.link(parent.node.key, key("knowledge_version", r.id), "HAS_VERSION", r.id)
    accounts = {r.id: r.organization_unit_id for r in await rows(UserAccount)}
    for r in await rows(KnowledgeRun):
        account_org = accounts.get(r.requested_by)
        if account_org:
            c.add(
                "knowledge_run",
                r,
                "引用问答",
                r.status,
                account_org,
                r.requested_by,
                P.KNOWLEDGE_READ,
            )
            for citation in r.trace.get("evidence", []):
                if citation.get("version_id"):
                    c.link(
                        key("knowledge_version", citation["version_id"]),
                        key("knowledge_run", r.id),
                        "CITED_BY",
                        r.id,
                    )
    for r in await rows(AgentEvidence):
        for version in [
            r.content.get("version_id"),
            *[x.get("version_id") for x in r.content.get("evidence", []) if isinstance(x, dict)],
        ]:
            if version:
                c.link(
                    key("knowledge_version", version), key("agent_run", r.run_id), "CITED_BY", r.id
                )
        if r.source_server == "business-graph":
            for identifier in r.content.get("validation", {}).get("nodes", {}):
                source = c.records.get(identifier)
                if source and source.node.kind in {"knowledge_version", "experience"}:
                    c.link(
                        identifier,
                        key("agent_run", r.run_id),
                        "CITED_BY" if source.node.kind == "knowledge_version" else "USED_IN",
                        r.id,
                    )
        if r.source_server == "experience-memory":
            c.link(
                key("experience", r.source_resource), key("agent_run", r.run_id), "USED_IN", r.id
            )
    source_kinds = {
        "handover": "audio",
        "audio": "audio",
        "audio_record": "audio",
        "audio_transcript": "audio",
        "manual_operation": "manual_record",
        "meeting": "meeting",
        "inspection": "inspection",
        "inspection_finding": "inspection_finding",
        "production_event": "event",
    }
    for r in await rows(EventSourceLink):
        source = source_kinds.get(r.source_type)
        if source:
            c.link(
                key(source, r.source_id),
                key("event", r.event_id),
                "SOURCE_OF",
                f"{r.id}@{r.source_version_id}",
            )
    for record in list(c.records.values()):
        if isinstance(record.row, Hazard):
            r = record.row
            source = source_kinds.get(r.source_type)
            if r.source_type == "handover" and key("audio", r.source_id) not in c.records:
                source = "manual_record"
            if source:
                c.link(key(source, r.source_id), record.node.key, "SOURCE_OF", r.id)
    for r in await rows(HazardWorkOrderLink):
        c.link(
            key("hazard", r.hazard_id), key("work_order", r.work_order_id), "REMEDIATED_BY", r.id
        )
    c.publications = await rows(MeetingKnowledgePublication)
    for r in c.publications:
        c.link(
            key("meeting", r.meeting_id),
            key("knowledge_version", r.knowledge_version_id),
            "PUBLISHED_AS",
            r.id,
        )
    for r in await rows(EquipmentAssociation):
        target = c.records.get(key(r.record_kind, r.record_id))
        if r.active and target and r.source_revision == binding_revision(target.row):
            c.link(key("equipment", r.equipment_id), target.node.key, "AFFECTS", r.id)
            c.link(key("failure_mode", r.failure_mode_id), target.node.key, "CLASSIFIES", r.id)
    return c

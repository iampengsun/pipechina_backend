"""Read graph paths, then revalidate each node and edge against current SQL facts and ACLs."""

from sqlalchemy import func, select

from app.modules.operational_graph.application.catalog import build_catalog, key
from app.modules.operational_graph.domain.models import GraphChange, GraphProjection
from app.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.security.authorization.permissions import Permissions as P


def graph_client(manager):
    graph = manager.get().business_graph
    if graph is None:
        raise ConflictError("business graph is not configured")
    return graph


async def projection_status(session):
    row = await session.get(GraphProjection, 1, populate_existing=True)
    pending = await session.scalar(select(func.count()).select_from(GraphChange))
    return {
        "generation": row.generation if row else None,
        "synced_at": row.synced_at if row else None,
        "pending_changes": pending,
        "last_error": row.last_error if row else None,
        "counts": row.counts if row else {},
        "ready": bool(row and row.generation),
    }


async def checked_paths(session, user, graph, root, *, depth=4, limit=100, dependencies_only=False):
    if not user.has_permission(P.GRAPH_READ):
        raise PermissionDeniedError(P.GRAPH_READ)
    catalog = await build_catalog(session)
    if root not in catalog.records:
        raise NotFoundError("graph_record", root)
    if not catalog.can_read(root, user):
        raise PermissionDeniedError(P.GRAPH_READ)
    state = await projection_status(session)
    if not state["ready"]:
        raise ConflictError("business graph needs initial synchronization")
    allowed = [identifier for identifier in catalog.records if catalog.can_read(identifier, user)]
    paths = await graph.paths(
        state["generation"], root, allowed, depth, limit + 1, dependencies_only=dependencies_only
    )
    valid = []
    for path in paths:
        if (
            path.nodes[0] != root
            or len(path.nodes) != len(path.edges) + 1
            or len(set(path.nodes)) != len(path.nodes)
        ):
            continue
        if any(
            identifier not in catalog.records or not catalog.can_read(identifier, user)
            for identifier in path.nodes
        ):
            continue
        okay = True
        for i, edge_id in enumerate(path.edges):
            edge = catalog.edges.get(edge_id)
            if edge is None or {edge.source, edge.target} != {path.nodes[i], path.nodes[i + 1]}:
                okay = False
                break
            if dependencies_only and (
                edge.source != path.nodes[i] or edge.target != path.nodes[i + 1]
            ):
                okay = False
                break
        if okay:
            valid.append(path)
    limited = len(paths) > limit
    return (
        catalog,
        valid[:limit],
        {
            "generation": state["generation"],
            "synced_at": state["synced_at"].isoformat() if state["synced_at"] else None,
            "source_changes_pending": bool(state["pending_changes"]),
            "limited": limited,
            "complete": not limited and not state["pending_changes"],
        },
    )


async def trace(session, user, graph, kind, record_id, *, depth=4, limit=100):
    root = key(kind, record_id)
    catalog, paths, state = await checked_paths(
        session, user, graph, root, depth=depth, limit=limit
    )
    nodes = {root, *[identifier for path in paths for identifier in path.nodes]}
    edges = {identifier for path in paths for identifier in path.edges}
    return {
        "root": root,
        "nodes": [catalog.view(k) for k in sorted(nodes)],
        "edges": [catalog.edges[k].model_dump() for k in sorted(edges)],
        "paths": [p.model_dump() for p in paths],
        "projection": state,
        "validation": validation_snapshot(catalog, paths, root),
        "interpretation": "关系表示已记录的业务引用或人工确认关联，不代表因果判断。",
    }


def validation_snapshot(catalog, paths, root):
    identifiers = {root, *[k for p in paths for k in p.nodes]}
    return {
        "nodes": {k: catalog.records[k].node.revision for k in sorted(identifiers)},
        "edges": sorted({k for p in paths for k in p.edges}),
    }


async def validate_evidence(session, user, content):
    if not user.has_permission(P.GRAPH_READ):
        raise PermissionDeniedError(P.GRAPH_READ)
    catalog = await build_catalog(session)
    snapshot = content.get("validation", {})
    if not snapshot.get("nodes"):
        raise ConflictError("graph evidence has no source revisions")
    for identifier, revision in snapshot["nodes"].items():
        if not catalog.can_read(identifier, user):
            raise PermissionDeniedError(P.GRAPH_READ)
        if catalog.records[identifier].node.revision != revision:
            raise ConflictError("graph evidence source changed")
    if any(k not in catalog.edges for k in snapshot.get("edges", [])):
        raise ConflictError("graph evidence relationship changed")

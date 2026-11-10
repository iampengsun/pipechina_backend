"""Role-specific evidence views; omitted content remains addressable by evidence ID."""

import json
from datetime import UTC, datetime

from app.shared.errors import ConflictError


def current_evidence(items, *, as_of=None, include_expired=False):
    """Choose the newest spatial observation per operation and station selection."""
    items = list(items)
    now = as_of or datetime.now(UTC)
    latest: dict = {}
    other = []
    for item in items:
        if item.source_server != "site-context":
            other.append(item)
            continue
        content = item.content
        stations = tuple(ref["station_id"] for ref in content.get("location_refs", []))
        if not stations and content.get("station_id"):
            stations = (content["station_id"],)
        key = (content.get("event_id"), content.get("operation"), stations)
        observed = datetime.fromisoformat(content["retrieved_at"])
        if as_of is not None and observed > as_of:
            continue
        order = (observed, str(item.id))
        previous = latest.get(key)
        if previous is None or order > previous[0]:
            latest[key] = (order, item)
    for _, item in latest.values():
        expires = datetime.fromisoformat(item.content["valid_until"])
        if include_expired or expires > now:
            other.append(item)
    ids = {item.id for item in other}
    return [item for item in items if item.id in ids]


def evidence_view(items, *, limit=16000, required=()):
    ordered = sorted(
        items,
        key=lambda e: (
            str(e.id) not in set(required),
            e.source_server == "experience-memory",
            str(e.id),
        ),
    )
    result = []
    remaining = limit
    for item in ordered:
        content = item.content
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True)
        if len(encoded) > remaining:
            content = {
                "preview": str(content.get("text") or content.get("title") or "")[
                    : min(500, max(remaining, 0))
                ],
                "evidence_id": str(item.id),
                "content_hash": item.content_hash,
                "offloaded": True,
                "read_tool": "read_investigation_evidence",
            }
        else:
            remaining -= len(encoded)
        result.append(
            {
                "evidence_id": str(item.id),
                "source": item.source_server,
                "version": item.source_version,
                "content": content,
            }
        )
    if len(json.dumps(result, ensure_ascii=False)) > limit + 12000:
        raise ConflictError("evidence manifest exceeds context capacity")
    return result


def role_context(role, payload, items, *, limit=16000, as_of=None):
    items = current_evidence(items, as_of=as_of)
    result = dict(payload)
    if role in {
        "multi_agent_evidence",
        "multi_agent_risk",
        "multi_agent_plan",
        "multi_agent_critic",
    }:
        evidence = result.get("evidence", {})
        ids = set(
            result.get(
                "valid_evidence",
                (evidence.get("evidence_ids") or []) if isinstance(evidence, dict) else [],
            )
            or []
        )
        selected = [item for item in items if not ids or str(item.id) in ids]
        result["evidence_sources"] = evidence_view(
            selected, limit=limit, required=result.get("risk", {}).get("evidence_ids", [])
        )
        if isinstance(evidence, dict) and "facts" in evidence:
            result["evidence"] = {
                **evidence,
                "evidence_ids": [source["evidence_id"] for source in result["evidence_sources"]],
                "facts": [
                    "原始事实见证据 " + source["evidence_id"]
                    for source in result["evidence_sources"]
                ],
            }
        if role == "multi_agent_evidence":
            result["evidence"] = [row["content"] for row in result["evidence_sources"]]
    return result

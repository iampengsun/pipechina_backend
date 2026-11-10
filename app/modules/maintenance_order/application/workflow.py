from functools import lru_cache
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class MaintenanceState(TypedDict, total=False):
    approved: bool
    review_reason: str
    decision: str


def route_review(state: MaintenanceState) -> str:
    return "approved" if state.get("approved") else "rejected"


def mark_approved(state: MaintenanceState) -> MaintenanceState:
    return {"approved": True, "review_reason": state["review_reason"], "decision": "approved"}


def mark_rejected(state: MaintenanceState) -> MaintenanceState:
    return {"approved": False, "review_reason": state["review_reason"], "decision": "rejected"}


@lru_cache(maxsize=1)
def build_maintenance_graph():
    builder = StateGraph(MaintenanceState)
    builder.add_node("approved", mark_approved)
    builder.add_node("rejected", mark_rejected)
    builder.add_conditional_edges(
        START, route_review, {"approved": "approved", "rejected": "rejected"}
    )
    builder.add_edge("approved", END)
    builder.add_edge("rejected", END)
    return builder.compile()


async def resolve_maintenance_review(*, approved: bool, reason: str) -> str:
    """Run deterministic LangGraph routing after a human decision has been supplied."""
    state = await build_maintenance_graph().ainvoke({"approved": approved, "review_reason": reason})
    return str(state["decision"])

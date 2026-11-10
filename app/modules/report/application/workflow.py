import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.ports.models import ReportDraft
from app.ports.text import TextLLMProvider


class ReportState(TypedDict, total=False):
    source_items: list[dict[str, Any]]
    chunks: list[list[dict[str, Any]]]
    partials: list[dict[str, Any]]
    result: dict[str, Any]


def partition(state: ReportState) -> ReportState:
    items = state.get("source_items", [])
    return {"chunks": [items[index : index + 20] for index in range(0, len(items), 20)] or [[]]}


def map_deterministic(state: ReportState) -> ReportState:
    partials = [
        {"count": len(chunk), "source_ids": [str(item.get("id")) for item in chunk]}
        for chunk in state.get("chunks", [])
    ]
    return {"partials": partials}


def reduce_deterministic(state: ReportState) -> ReportState:
    partials = state.get("partials", [])
    return {
        "result": {
            "source_count": sum(item["count"] for item in partials),
            "source_ids": [source for item in partials for source in item["source_ids"]],
        }
    }


def build_report_graph():
    builder = StateGraph(ReportState)
    builder.add_node("partition", partition)
    builder.add_node("map", map_deterministic)
    builder.add_node("reduce", reduce_deterministic)
    builder.add_edge(START, "partition")
    builder.add_edge("partition", "map")
    builder.add_edge("map", "reduce")
    builder.add_edge("reduce", END)
    return builder.compile()


async def generate_report_map_reduce(
    provider: TextLLMProvider,
    source_items: list[dict[str, Any]],
    report_type: str,
    *,
    system_prompt: str = (
        "你是油气管网生产运行报告助手。只能使用输入中的已确认来源生成草稿，"
        "不得补造事实；所有采用的来源必须出现在 source_ids，疑点放入 pending_facts。"
    ),
    user_template: str = "{input}",
) -> ReportDraft:
    """Partition with LangGraph, map through the model, then reduce deterministically."""
    state = await build_report_graph().ainvoke({"source_items": source_items})
    chunks = state["chunks"]
    drafts = [
        await provider.generate_structured(
            operation="report_generation",
            system_prompt=system_prompt,
            user_prompt=user_template.format(
                input=json.dumps({"report_type": report_type, "sources": chunk}, ensure_ascii=False)
            ),
            response_model=ReportDraft,
        )
        for chunk in chunks
    ]
    if len(drafts) == 1:
        return drafts[0]

    headings = {heading for draft in drafts for heading in draft.sections}
    sections = {
        heading: "\n".join(
            draft.sections[heading] for draft in drafts if draft.sections.get(heading)
        )
        for heading in sorted(headings)
    }
    pending_facts = list(dict.fromkeys(fact for draft in drafts for fact in draft.pending_facts))
    return ReportDraft(
        title=drafts[0].title,
        sections=sections,
        source_ids=state["result"]["source_ids"],
        pending_facts=pending_facts,
    )

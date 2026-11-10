import pytest

from app.infrastructure.providers.fake import FakeTextLLMProvider
from app.modules.maintenance_order.application.workflow import resolve_maintenance_review
from app.modules.report.application.workflow import build_report_graph, generate_report_map_reduce


def test_report_graph_partitions_and_reduces_sources() -> None:
    graph = build_report_graph()
    sources = [{"id": index} for index in range(45)]
    result = graph.invoke({"source_items": sources})
    assert len(result["chunks"]) == 3
    assert result["result"]["source_count"] == 45
    assert len(result["result"]["source_ids"]) == 45


@pytest.mark.asyncio
async def test_report_map_reduce_preserves_all_source_ids() -> None:
    sources = [{"id": str(index), "type": "test"} for index in range(45)]
    result = await generate_report_map_reduce(FakeTextLLMProvider(), sources, "daily")
    assert len(result.source_ids) == 45
    assert result.source_ids[0] == "0"
    assert result.source_ids[-1] == "44"
    assert "共纳入 20 条" in result.sections["重点事件"]
    assert "共纳入 5 条" in result.sections["重点事件"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("approved", "expected"), [(True, "approved"), (False, "rejected")])
async def test_maintenance_graph_routes_human_decision(approved: bool, expected: str) -> None:
    assert await resolve_maintenance_review(approved=approved, reason="人工复核") == expected

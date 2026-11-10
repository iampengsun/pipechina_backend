"""An actual investigation tool call uses published knowledge and persists provenance."""

import hashlib
import json

from app.infrastructure.providers.fake import FakeTextLLMProvider
from tests.helpers import create_confirmed_event


def publish_manual(client):
    prefix = "/api/v1/knowledge"
    base_id = client.post(f"{prefix}/bases", json={"name": "阀门规范"}).json()["id"]
    document = client.post(
        f"{prefix}/documents",
        json={"base_id": base_id, "code": "VALVE-001", "title": "阀门检查规范", "kind": "manual"},
    ).json()["id"]
    content = "阀门出现渗漏时，应设置警戒并由专业人员复核压力。".encode()
    version = client.post(
        f"{prefix}/documents/{document}/versions",
        json={
            "filename": "manual.txt",
            "mime_type": "text/plain",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "effective_from": "2026-01-01T00:00:00Z",
        },
    ).json()
    version_id = version["version"]["id"]
    client.portal.call(
        client.app.state.providers.storage.put_bytes,
        version["upload"]["object_key"],
        content,
        "text/plain",
    )
    client.post(f"{prefix}/versions/{version_id}/uploads:complete").raise_for_status()
    client.post(f"{prefix}/versions/{version_id}:process").raise_for_status()
    client.post(
        f"{prefix}/versions/{version_id}:review", json={"approved": True, "reason": "人工核验通过"}
    ).raise_for_status()
    client.post(f"{prefix}/versions/{version_id}:publish").raise_for_status()
    return version_id


def test_agent_calls_rag_and_cannot_read_withdrawn_knowledge(client, monkeypatch):
    version_id = publish_manual(client)
    original = FakeTextLLMProvider.generate_structured

    async def generate(self, *, operation, system_prompt, user_prompt, response_model):
        if operation == "multi_agent_evidence":
            payload = json.loads(user_prompt)
            assert "search_knowledge" in {tool["name"] for tool in payload["tools"]}
            return response_model.model_validate(
                {
                    "action": "search_knowledge" if payload["remaining_steps"] == 6 else "finish",
                    "query": "阀门",
                    "unknowns": [],
                }
            )
        return await original(
            self,
            operation=operation,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
        )

    monkeypatch.setattr(FakeTextLLMProvider, "generate_structured", generate)
    event = create_confirmed_event(client)["event_id"]
    result = client.post(f"/api/v1/events/{event}:classify")
    assert result.status_code == 202, result.text
    run_id = result.json()["agent_run_id"]
    evidence = client.get(f"/api/v1/agent-runs/{run_id}/evidence").json()
    sources = [row for row in evidence if row["source_server"] == "knowledge-rag"]
    assert sources and sources[0]["source_version"] == version_id
    assert sources[0]["content"]["code"] == "VALVE-001"
    trace = client.get(f"/api/v1/agent-runs/{run_id}/trace").json()
    assert any(step["tool_name"] == "search_knowledge" for step in trace["steps"])
    client.post(
        f"/api/v1/knowledge/versions/{version_id}:withdraw", json={"reason": "规范过期撤回"}
    ).raise_for_status()
    assert client.get(f"/api/v1/agent-runs/{run_id}/evidence").status_code == 403
    assert client.get(f"/api/v1/agent-runs/{run_id}").status_code == 403

import hashlib
from functools import partial
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.scopes import ScopeType
from tests.security_helpers import authenticated_client, provision_test_identity, request_mock_token


@pytest.mark.parametrize("fail_vector_once", [False, True])
def test_knowledge_publication_answer_withdrawal(settings, fail_vector_once):
    settings.knowledge_backend = "fake"
    with authenticated_client(
        settings, username="admin", permissions={Permissions.ALL}, scope_type=ScopeType.GLOBAL
    ) as client:
        prefix = "/api/v1/knowledge"
        response = client.post(f"{prefix}/bases", json={"name": "设备资料"})
        assert response.status_code == 201, response.text
        base_id = response.json()["id"]
        response = client.post(
            f"{prefix}/documents",
            json={
                "base_id": base_id,
                "code": "DEMO-001",
                "title": "演示设备规范",
                "kind": "manual",
            },
        )
        assert response.status_code == 201, response.text
        document_id = response.json()["id"]
        content = "# 演示设备\n仅用于测试，演示设备的标识是 DEMO-001。".encode()
        response = client.post(
            f"{prefix}/documents/{document_id}/versions",
            json={
                "filename": "demo.txt",
                "mime_type": "text/plain",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "effective_from": "2026-01-01T00:00:00Z",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        version_id = data["version"]["id"]
        client.portal.call(
            client.app.state.providers.storage.put_bytes,
            data["upload"]["object_key"],
            content,
            "text/plain",
        )
        response = client.post(f"{prefix}/versions/{version_id}/uploads:complete")
        assert response.status_code == 200, response.text
        if fail_vector_once:
            vector_index = client.app.state.knowledge.get().vector
            original_upsert = vector_index.upsert
            vector_index.upsert = AsyncMock(side_effect=RuntimeError("simulated outage"))
            with pytest.raises(ExceptionGroup):
                client.post(f"{prefix}/versions/{version_id}:process")
            state = client.get(f"{prefix}/documents/{document_id}/versions").json()[0]
            assert state["status"] == "failed"
            assert state["index_state"]["fulltext"] == "ready"
            assert state["index_state"].get("vector") != "ready"
            vector_index.upsert = original_upsert
        response = client.post(f"{prefix}/versions/{version_id}:process")
        assert response.status_code == 202, response.text
        artifact = client.get(f"{prefix}/versions/{version_id}/artifact").json()["artifact"]
        block = artifact["blocks"][0]
        response = client.post(
            f"{prefix}/versions/{version_id}/artifact:correct",
            json={
                "reason": "校核识别文本",
                "corrections": [{"block_id": block["id"], "text": block["text"]}],
            },
        )
        assert response.status_code == 200, response.text
        response = client.post(f"{prefix}/versions/{version_id}:process")
        assert response.status_code == 202, response.text
        query = {"question": "演示设备标识是什么？", "base_ids": [base_id]}
        assert not client.post(f"{prefix}/search", json=query).json()["evidence"]
        response = client.post(
            f"{prefix}/versions/{version_id}:review",
            json={"approved": True, "reason": "测试核对通过"},
        )
        assert response.status_code == 200, response.text
        response = client.post(f"{prefix}/versions/{version_id}:publish")
        assert response.status_code == 200, response.text
        response = client.post(f"{prefix}/runs", json=query)
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        result = client.get(f"{prefix}/runs/{run_id}")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "succeeded"
        assert result.json()["answer"]["claims"][0]["citations"]
        events = client.get(f"{prefix}/runs/{run_id}/events")
        assert events.status_code == 200, events.text
        assert "event: verified_claim" in events.text
        assert "event: completed" in events.text
        evidence = client.get(f"{prefix}/versions/{version_id}/evidence").json()
        response = client.post(
            f"{prefix}/evaluations",
            json={
                "name": "验收",
                "split": "held_out",
                "base_ids": [base_id],
                "cases": [
                    {
                        "question": query["question"],
                        "relevant_evidence_ids": [evidence[0]["id"]],
                        "should_refuse": False,
                    }
                ],
            },
        )
        assert response.status_code == 202, response.text
        evaluation = client.get(f"{prefix}/evaluations/{response.json()['id']}").json()
        assert evaluation["status"] == "succeeded", evaluation
        assert evaluation["results"]["averages"]["recall"] == 1
        assert evaluation["results"]["averages"]["refusal_accuracy"] == 1
        admin_auth = client.headers["Authorization"]
        client.portal.call(
            partial(
                provision_test_identity,
                client.app,
                settings,
                username="peter",
                permissions={Permissions.KNOWLEDGE_READ},
                scope_type=ScopeType.OWN_ORG,
            )
        )
        reader_auth = "Bearer " + request_mock_token(settings, username="peter")
        client.headers["Authorization"] = reader_auth
        reader_run = client.post(f"{prefix}/runs", json=query)
        assert reader_run.status_code == 202, reader_run.text
        reader_run_id = reader_run.json()["id"]
        assert client.get(f"{prefix}/runs/{reader_run_id}").status_code == 200
        client.headers["Authorization"] = admin_auth
        response = client.post(
            f"{prefix}/documents/{document_id}/acl",
            json={"expected_version": 1, "reader_ids": [str(uuid4())]},
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = reader_auth
        assert client.get(f"{prefix}/runs/{reader_run_id}").status_code == 403
        assert not client.post(f"{prefix}/search", json=query).json()["evidence"]
        client.headers["Authorization"] = admin_auth
        response = client.post(
            f"{prefix}/versions/{version_id}:withdraw", json={"reason": "测试撤回"}
        )
        assert response.status_code == 200, response.text
        assert client.get(f"{prefix}/runs/{run_id}").status_code == 403
        assert not client.post(f"{prefix}/search", json=query).json()["evidence"]

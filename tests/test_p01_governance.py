from functools import partial
from uuid import UUID

from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.scopes import ScopeType
from tests.helpers import create_confirmed_event
from tests.security_helpers import provision_test_identity, request_mock_token
from tests.test_meeting_flow import create_transcribed_meeting


def test_event_edit_requires_reconfirmation_and_rejects_stale_review(client):
    event_id = create_confirmed_event(client)["event_id"]
    event = client.get(f"/api/v1/events/{event_id}").json()
    classified = client.post(f"/api/v1/events/{event_id}:classify").json()
    workflow_url = f"/api/v1/workflows/{classified['workflow_id']}"
    assert (
        client.post(
            workflow_url + ":review",
            json={"approved": True, "reason": "使用旧页面版本", "expected_version": 999},
        ).status_code
        == 409
    )
    payload = {
        "expected_version_id": event["current_version_id"],
        "title": "复核后的阀门异常",
        "event_type": "equipment_leak",
        "severity": "high",
        "description": "重新核实后发现异常影响扩大。",
    }
    changed = client.put(f"/api/v1/events/{event_id}", json=payload)
    assert changed.status_code == 200, changed.text
    assert client.get(f"/api/v1/events/{event_id}").json()["business_status"] == "candidate"
    assert (
        client.post(
            f"/api/v1/events/{event_id}:confirm",
            json={"expected_version_id": event["current_version_id"]},
        ).status_code
        == 409
    )
    assert client.put(f"/api/v1/events/{event_id}", json=payload).status_code == 409
    assert client.post(f"/api/v1/events/{event_id}:classify").status_code == 409
    assert (
        client.post(
            workflow_url + ":review",
            json={"approved": True, "reason": "试图批准旧调查", "expected_version": 1},
        ).status_code
        == 409
    )
    rejected = client.post(
        workflow_url + ":review",
        json={"approved": False, "reason": "事件版本已变化，关闭旧调查", "expected_version": 1},
    )
    assert rejected.status_code == 200, rejected.text
    assert (
        client.post(
            f"/api/v1/events/{event_id}:confirm",
            json={"expected_version_id": changed.json()["version_id"]},
        ).status_code
        == 200
    )


def test_meeting_knowledge_keeps_reader_scope_and_rejects_widening(client, settings):
    me = client.get("/api/v1/auth/me").json()
    meeting_id = create_transcribed_meeting(client)

    changed = client.put(
        f"/api/v1/meetings/{meeting_id}/acl",
        json={"expected_version": 1, "reader_ids": [me["user_id"]], "reason": "收紧会议资料访问"},
    )
    assert changed.status_code == 200, changed.text
    stale = client.put(
        f"/api/v1/meetings/{meeting_id}/acl",
        json={"expected_version": 1, "reader_ids": [], "reason": "旧版本不能覆盖权限"},
    )
    assert stale.status_code == 409
    base = client.post("/api/v1/knowledge/bases", json={"name": "组织内知识库"}).json()["id"]
    published = client.post(
        f"/api/v1/meetings/{meeting_id}:publish-to-knowledge", json={"knowledge_base_id": base}
    )
    assert published.status_code == 202, published.text
    document_id = published.json()["knowledge_document_id"]

    async def read_document():
        from app.modules.knowledge.domain.models import KnowledgeDocument

        async with client.app.state.database.session_factory() as session:
            doc = await session.get(KnowledgeDocument, UUID(document_id))
            return doc.reader_ids, doc.acl_version

    readers, version = client.portal.call(read_document)
    assert readers == [me["user_id"]]
    assert (
        client.post(
            f"/api/v1/knowledge/documents/{document_id}/acl",
            json={"expected_version": version, "reader_ids": []},
        ).status_code
        == 409
    )
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
    client.headers["Authorization"] = "Bearer " + request_mock_token(settings, username="peter")
    assert client.get(f"/api/v1/knowledge/documents/{document_id}/versions").status_code == 403


def test_meeting_acl_revocation_applies_to_existing_knowledge_answers(client, settings):
    from tests.security_helpers import request_mock_token

    admin_headers = dict(client.headers)
    me = client.get("/api/v1/auth/me").json()
    meeting = create_transcribed_meeting(client)
    base = client.post("/api/v1/knowledge/bases", json={"name": "会议动态授权测试"}).json()["id"]
    publication = client.post(
        f"/api/v1/meetings/{meeting}:publish-to-knowledge", json={"knowledge_base_id": base}
    )
    assert publication.status_code == 202, publication.text
    version = publication.json()["knowledge_version_id"]
    assert (
        client.post(
            f"/api/v1/knowledge/versions/{version}:review",
            json={"approved": True, "reason": "合成测试核实"},
        ).status_code
        == 200
    )
    assert client.post(f"/api/v1/knowledge/versions/{version}:publish").status_code == 200
    client.portal.call(
        partial(
            provision_test_identity,
            client.app,
            settings,
            username="peter",
            permissions={Permissions.KNOWLEDGE_READ, Permissions.MEETING_READ},
            scope_type=ScopeType.OWN_ORG,
        )
    )
    peter = {"Authorization": "Bearer " + request_mock_token(settings, username="peter")}
    assert client.get(f"/api/v1/meetings/{meeting}", headers=peter).status_code == 200
    asked = client.post(
        "/api/v1/knowledge/runs",
        headers=peter,
        json={"question": "会议记录了什么？", "base_ids": [base]},
    )
    assert asked.status_code == 202, asked.text
    answer_id = asked.json()["id"]
    assert client.get(f"/api/v1/knowledge/runs/{answer_id}", headers=peter).status_code == 200
    changed = client.put(
        f"/api/v1/meetings/{meeting}/acl",
        headers=admin_headers,
        json={"expected_version": 1, "reader_ids": [me["user_id"]], "reason": "撤回先前读者权限"},
    )
    assert changed.status_code == 200, changed.text
    assert client.get(f"/api/v1/meetings/{meeting}", headers=peter).status_code == 403
    assert client.get(f"/api/v1/knowledge/runs/{answer_id}", headers=peter).status_code == 403

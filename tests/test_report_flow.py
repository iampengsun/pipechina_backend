from datetime import date

from fastapi.testclient import TestClient

from tests.helpers import create_confirmed_event, create_confirmed_handover


def test_report_generate_review_publish_export_and_withdraw(client: TestClient) -> None:
    create_confirmed_handover(client)
    create_confirmed_event(client)
    created = client.post(
        "/api/v1/reports",
        json={
            "report_type": "daily",
            "business_date": str(date.today()),
            "timezone": "Asia/Shanghai",
            "scope_filter": {"shift": "all"},
        },
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["id"]

    generated = client.post(f"/api/v1/reports/{report_id}:generate")
    assert generated.status_code == 202, generated.text
    report = client.get(f"/api/v1/reports/{report_id}").json()
    assert report["current_version"]["title"] == "生产运行日报"
    assert report["current_version"]["review_status"] == "draft"

    submitted = client.post(f"/api/v1/reports/{report_id}:submit-review")
    assert submitted.json()["status"] == "pending_review"
    reviewed = client.post(
        f"/api/v1/reports/{report_id}:review",
        json={"approved": True, "reason": "来源和事实已核验"},
    )
    assert reviewed.json()["status"] == "approved"
    published = client.post(f"/api/v1/reports/{report_id}:publish")
    assert published.json()["status"] == "published"

    exported = client.post(f"/api/v1/reports/{report_id}/exports", json={"format": "docx"})
    assert exported.status_code == 202, exported.text
    assert exported.json()["status"] == "succeeded"
    export_id = exported.json()["export_id"]
    export = client.get(f"/api/v1/reports/{report_id}/exports/{export_id}")
    assert export.json()["download_url"].startswith("memory://download/")

    withdrawn = client.post(
        f"/api/v1/reports/{report_id}:withdraw", json={"reason": "发现来源记录需要更正"}
    )
    assert withdrawn.json()["status"] == "withdrawn"


def test_duplicate_report_scope_is_rejected(client: TestClient) -> None:
    payload = {
        "report_type": "daily",
        "business_date": str(date.today()),
        "timezone": "Asia/Shanghai",
        "scope_filter": {"station": "A"},
    }
    assert client.post("/api/v1/reports", json=payload).status_code == 201
    duplicate = client.post("/api/v1/reports", json=payload)
    assert duplicate.status_code == 409

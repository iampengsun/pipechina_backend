import re

from fastapi.testclient import TestClient
from sqlalchemy import JSON
from sqlalchemy.dialects import postgresql, sqlite

from app.shared.db import Base


def normalize(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{id}", path)


def test_contract_has_documented_access_control_and_knowledge_operations(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    http_methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    operations = {
        (method.upper(), normalize(path))
        for path, item in schema["paths"].items()
        for method in item
        if method in http_methods and method not in {"head", "options"}
    }
    existing_operations = {op for op in operations if "/knowledge" not in op[1]}
    assert len(existing_operations) == 193
    assert len(Base.metadata.tables) == 85

    required = {
        ("GET", "/api/v1/event-extractions/{id}/results"),
        ("POST", "/api/v1/knowledge/evaluations/{id}:review"),
        ("POST", "/api/v1/evaluation-improvements/{id}:assign"),
        ("GET", "/api/v1/evaluation-improvements/reminders"),
        ("GET", "/api/v1/evaluation-improvements/participants"),
        ("GET", "/api/v1/evaluation-improvements/data/evaluations"),
        ("GET", "/api/v1/evaluation-improvements/data/review/{id}"),
        ("POST", "/api/v1/evaluation-improvements"),
        ("GET", "/api/v1/evaluation-improvements"),
        ("GET", "/api/v1/evaluation-improvements/{id}"),
        ("POST", "/api/v1/evaluation-improvements/{id}:implement"),
        ("POST", "/api/v1/evaluation-improvements/{id}:retest"),
        ("POST", "/api/v1/evaluation-improvements/{id}:accept"),
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/agent-evaluations"),
        ("POST", "/api/v1/meetings/{id}:generate-minutes"),
        ("DELETE", "/api/v1/meetings/{id}/recording"),
        ("POST", "/api/v1/work-orders/{id}/attachments/{id}/uploads:complete"),
        ("POST", "/api/v1/knowledge/runs"),
        ("GET", "/api/v1/knowledge/runs/{id}/events"),
        ("POST", "/api/v1/knowledge/versions/{id}:publish"),
        ("POST", "/api/v1/audio-records/{id}:transcribe"),
        ("POST", "/api/v1/meetings/{id}:transcribe"),
        ("POST", "/api/v1/meetings/{id}:publish-to-knowledge"),
        ("POST", "/api/v1/events/{id}:classify"),
        ("GET", "/api/v1/agent-runs/{id}"),
        ("GET", "/api/v1/agent-runs/{id}/evidence"),
        ("GET", "/api/v1/agent-runs/{id}/quality"),
        ("GET", "/api/v1/agent-runs/{id}/trace"),
        ("POST", "/api/v1/agent-runs/{id}:cancel"),
        ("POST", "/api/v1/workflows/{id}:review"),
        ("POST", "/api/v1/inspections/{id}:analyze"),
        ("POST", "/api/v1/hazards"),
        ("GET", "/api/v1/hazards/{id}"),
        ("POST", "/api/v1/hazards/{id}:assess"),
        ("POST", "/api/v1/hazards/{id}:submit-rectification"),
        ("POST", "/api/v1/hazards/{id}:accept"),
        ("POST", "/api/v1/reports/{id}:publish"),
        ("GET", "/api/v1/reports/{id}/exports/{id}"),
        ("GET", "/api/v1/admin/access/permissions"),
        ("POST", "/api/v1/admin/access/role-assignments"),
        ("GET", "/api/v1/admin/access/users/{id}/effective-access"),
    }
    assert required <= operations


def test_openapi_contains_business_tags(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/audio-records" in paths
    assert "/api/v1/meetings" in paths
    assert "/api/v1/work-orders/{order_id}:dispatch" in paths
    assert "/api/v1/reports/{report_id}:withdraw" in paths


def test_json_documents_use_jsonb_on_postgres_and_json_on_sqlite() -> None:
    columns = [
        column
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, JSON)
    ]
    assert len(columns) == 73
    assert {column.type.compile(dialect=postgresql.dialect()) for column in columns} == {"JSONB"}
    assert {column.type.compile(dialect=sqlite.dialect()) for column in columns} == {"JSON"}

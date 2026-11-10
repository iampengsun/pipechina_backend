import pytest
from fastapi.testclient import TestClient

from app.bootstrap.config import Settings
from app.main import create_app
from tests.security_helpers import authenticated_client, request_mock_token


def test_authentication_is_required(anonymous_client: TestClient) -> None:
    response = anonymous_client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_current_user_and_health(client: TestClient) -> None:
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "admin"
    assert "*" in me.json()["permissions"]

    assert client.get("/health/live").json()["status"] == "ok"
    assert client.get("/health/ready").json()["status"] == "ready"


def test_frontend_origin_receives_explicit_cors_headers(client: TestClient) -> None:
    response = client.options(
        "/api/v1/auth/me",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "Authorization" in response.headers["access-control-allow-headers"]


def test_application_has_no_token_issuance_endpoint(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/auth/dev-token",
            json={"username": "admin", "password": "123456"},
        )
    assert response.status_code == 404


def test_application_config_has_no_local_jwt_signing_mode() -> None:
    assert "jwt_secret" not in Settings.model_fields
    assert "jwt_key_source" not in Settings.model_fields
    assert "enable_dev_token" not in Settings.model_fields


def test_production_rejects_non_tls_identity_provider() -> None:
    with pytest.raises(ValueError, match="JWT_ISSUER"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://app:secret@postgres:5432/pipechina",
        )


def test_complete_production_settings_are_accepted() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://app:secret@postgres:5432/pipechina",
        jwt_issuer="https://identity.example.test",
        jwt_audience="pipechina-backend",
        jwt_algorithm="RS256",
        jwt_jwks_url="https://identity.example.test/.well-known/jwks.json",
        text_provider="qwen",
        asr_provider="local_http",
        vision_provider="local_http",
        dashscope_api_key="remote-api-key",
        storage_provider="s3",
        s3_endpoint_url="https://objects.example.test",
        s3_public_endpoint_url="https://objects.example.test",
        s3_access_key="access-key",
        s3_secret_key="secret-key",
        s3_use_ssl=True,
    )

    assert settings.app_env == "production"


@pytest.mark.parametrize(
    "scenario",
    [
        "expired",
        "not_yet_valid",
        "wrong_audience",
        "wrong_issuer",
        "unknown_kid",
    ],
)
def test_invalid_external_tokens_are_rejected(
    settings: Settings,
    scenario: str,
) -> None:
    with authenticated_client(settings, username="peter", scenario=scenario) as client:
        response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


def test_unknown_external_identity_is_not_created_implicitly(settings: Settings) -> None:
    token = request_mock_token(settings, username="peter")
    with TestClient(
        create_app(settings),
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        response = client.get("/api/v1/auth/me")
    assert response.status_code == 403
    assert response.json()["code"] == "ACCOUNT_NOT_PROVISIONED"


def test_jwt_permission_claims_cannot_grant_database_permissions(
    settings: Settings,
) -> None:
    with authenticated_client(
        settings,
        username="peter",
        permissions=set(),
        scenario="forged_permissions",
    ) as client:
        response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["roles"] == []
    assert response.json()["permissions"] == []


def test_disabled_database_user_is_rejected_after_external_login(
    settings: Settings,
) -> None:
    with authenticated_client(settings, username="tom", active=False) as client:
        response = client.get("/api/v1/auth/me")
    assert response.status_code == 403
    assert response.json()["code"] == "ACCOUNT_DISABLED"

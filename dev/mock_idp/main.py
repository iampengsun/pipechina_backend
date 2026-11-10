from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Form, HTTPException
from pydantic import BaseModel

from dev.mock_idp.keys import SigningKeyStore

DEFAULT_ISSUER = "http://mock-idp:9001"
DEFAULT_AUDIENCE = "pipechina-backend"
DEFAULT_TOKEN_MINUTES = 60


@dataclass(frozen=True, slots=True)
class MockIdPSettings:
    issuer: str = DEFAULT_ISSUER
    audience: str = DEFAULT_AUDIENCE
    token_minutes: int = DEFAULT_TOKEN_MINUTES

    @classmethod
    def from_environment(cls) -> MockIdPSettings:
        return cls(
            issuer=os.getenv("MOCK_IDP_ISSUER", DEFAULT_ISSUER),
            audience=os.getenv("MOCK_IDP_AUDIENCE", DEFAULT_AUDIENCE),
            token_minutes=int(os.getenv("MOCK_IDP_TOKEN_MINUTES", str(DEFAULT_TOKEN_MINUTES))),
        )


class MockUser(BaseModel):
    username: str
    password: str
    subject: str
    display_name: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


def _load_users() -> dict[str, MockUser]:
    path = Path(__file__).with_name("users.json")
    records = json.loads(path.read_text(encoding="utf-8"))
    return {item["username"]: MockUser.model_validate(item) for item in records}


def create_mock_idp_app(settings: MockIdPSettings | None = None) -> FastAPI:
    configured = settings or MockIdPSettings.from_environment()
    users = _load_users()
    keys = SigningKeyStore()
    app = FastAPI(
        title="PipeChina development Mock IdP",
        description="Development-only external RS256 JWT issuer. Never deploy in production.",
    )
    app.state.settings = configured
    app.state.keys = keys

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/.well-known/jwks.json")
    async def jwks() -> dict[str, list[dict]]:
        return keys.jwks()

    @app.get("/.well-known/openid-configuration")
    async def discovery() -> dict[str, str | list[str]]:
        return {
            "issuer": configured.issuer,
            "token_endpoint": f"{configured.issuer}/token",
            "jwks_uri": f"{configured.issuer}/.well-known/jwks.json",
            "grant_types_supported": ["password"],
            "token_endpoint_auth_methods_supported": ["none"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    @app.post("/token", response_model=TokenResponse)
    async def token(
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        grant_type: Annotated[str, Form()] = "password",
        scenario: Annotated[
            Literal[
                "normal",
                "expired",
                "not_yet_valid",
                "wrong_audience",
                "wrong_issuer",
                "unknown_kid",
                "forged_permissions",
            ],
            Form(),
        ] = "normal",
    ) -> TokenResponse:
        user = users.get(username)
        if (
            grant_type != "password"
            or user is None
            or not secrets.compare_digest(user.password, password)
        ):
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_grant", "error_description": "invalid credentials"},
            )

        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=configured.token_minutes)
        not_before = now
        issuer = configured.issuer
        audience = configured.audience
        publish_key = True
        if scenario == "expired":
            expires_at = now - timedelta(minutes=1)
        elif scenario == "not_yet_valid":
            not_before = now + timedelta(minutes=5)
        elif scenario == "wrong_audience":
            audience = "wrong-audience"
        elif scenario == "wrong_issuer":
            issuer = "https://wrong-issuer.invalid"
        elif scenario == "unknown_kid":
            publish_key = False

        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": user.subject,
            "preferred_username": user.username,
            "name": user.display_name,
            "iat": int(now.timestamp()),
            "nbf": int(not_before.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        if scenario == "forged_permissions":
            claims.update({"roles": ["system_administrator"], "permissions": ["*"]})
        access_token = keys.sign(claims, publish_key=publish_key)
        return TokenResponse(
            access_token=access_token,
            expires_in=max(0, int((expires_at - now).total_seconds())),
        )

    @app.post("/admin/rotate-key")
    async def rotate_key() -> dict[str, str]:
        return {"kid": keys.rotate()}

    return app


app = create_mock_idp_app()

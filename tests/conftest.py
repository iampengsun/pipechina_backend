import os
import socket
import threading
import time
from collections.abc import Iterator
from urllib.request import urlopen

import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.bootstrap.config import Settings
from app.main import create_app
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.scopes import ScopeType
from dev.mock_idp.main import MockIdPSettings, create_mock_idp_app
from tests.security_helpers import authenticated_client


@pytest.fixture(scope="session")
def mock_idp_url() -> Iterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    app = create_mock_idp_app(MockIdPSettings(issuer=base_url, audience="pipechina-backend"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while True:
        try:
            with urlopen(f"{base_url}/health/live", timeout=0.2) as response:  # noqa: S310
                if response.status == 200:
                    break
        except OSError:
            if time.monotonic() >= deadline:
                server.should_exit = True
                thread.join(timeout=2)
                raise RuntimeError("Mock IdP failed to start") from None
            time.sleep(0.02)
    yield base_url
    server.should_exit = True
    thread.join(timeout=5)


TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://peter:123456@127.0.0.1:5432/pipechina_test",
    ),
)


def _reset_test_schema() -> None:
    sync_url = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")


@pytest.fixture
def settings(mock_idp_url: str) -> Settings:
    _reset_test_schema()
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=TEST_DATABASE_URL,
        jwt_issuer=mock_idp_url,
        jwt_audience="pipechina-backend",
        jwt_algorithm="RS256",
        jwt_jwks_url=f"{mock_idp_url}/.well-known/jwks.json",
        auto_create_schema=True,
        run_tasks_inline=True,
        knowledge_backend="fake",
        text_provider="fake",
        asr_provider="fake",
        vision_provider="fake",
        storage_provider="memory",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with authenticated_client(
        settings,
        username="admin",
        permissions={Permissions.ALL},
        scope_type=ScopeType.GLOBAL,
    ) as test_client:
        yield test_client


@pytest.fixture
def anonymous_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client

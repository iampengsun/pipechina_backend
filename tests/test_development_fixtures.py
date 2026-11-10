from types import SimpleNamespace
from uuid import uuid4

import pytest

from dev import acceptance_fixtures


@pytest.mark.parametrize(
    "environment,host",
    [
        ("production", "127.0.0.1"),
        ("development", "enterprise.example"),
        ("test", "database.internal"),
    ],
)
@pytest.mark.asyncio
async def test_acceptance_fixtures_never_seed_production_or_remote_databases(
    monkeypatch, environment, host
):
    settings = SimpleNamespace(
        app_env=environment, database_url=f"postgresql+asyncpg://user:secret@{host}/test"
    )
    monkeypatch.setattr(acceptance_fixtures, "get_settings", lambda: settings)
    with pytest.raises(ValueError, match="local development database"):
        await acceptance_fixtures.create_synthetic_order(uuid4())

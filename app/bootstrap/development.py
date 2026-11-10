"""Idempotent initialization of the project-owned local stack."""

import asyncio
import subprocess

from app.bootstrap.config import get_settings


def main():
    if get_settings().app_env != "development":
        raise RuntimeError("development bootstrap requires APP_ENV=development")
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    from scripts.bootstrap_knowledge_indexes import initialize
    from scripts.bootstrap_mock_idp_users import bootstrap

    asyncio.run(bootstrap())
    asyncio.run(initialize("v1"))
    from app.modules.operational_graph.application.tasks import synchronize_business_graph

    asyncio.run(synchronize_business_graph())


if __name__ == "__main__":
    main()

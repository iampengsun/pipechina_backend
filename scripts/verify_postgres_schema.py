"""校验 PostgreSQL 表结构和 Alembic 版本是否与当前代码一致。

用途：检查缺失表、多余表和迁移版本不一致；只读，不修改数据库。
时机：执行 Alembic 迁移后、启动项目前，或排查数据库结构问题时。
前置：`.env` 中 `DATABASE_URL` 指向需要检查的 PostgreSQL。
运行：`uv run python scripts/verify_postgres_schema.py`
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.bootstrap.model_registry  # noqa: F401
from app.bootstrap.config import get_settings
from app.shared.db import Base

EXPECTED_REVISION = "0003_station_locations"


async def verify_schema() -> None:
    database_url = get_settings().database_url
    if not database_url.startswith("postgresql+"):
        raise SystemExit("DATABASE_URL must be an async PostgreSQL SQLAlchemy URL")

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                    """
                )
            )
            actual_tables = {str(row[0]) for row in result}
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()

    expected_tables = set(Base.metadata.tables) | {"alembic_version"}
    missing = sorted(expected_tables - actual_tables)
    unexpected = sorted(actual_tables - expected_tables)
    if missing or unexpected:
        raise SystemExit(f"schema mismatch: missing={missing}, unexpected={unexpected}")
    if revision != EXPECTED_REVISION:
        raise SystemExit(f"revision mismatch: expected={EXPECTED_REVISION}, actual={revision}")

    print(
        f"PostgreSQL schema verified: {len(Base.metadata.tables)} application tables, "
        f"revision {revision}"
    )


if __name__ == "__main__":
    asyncio.run(verify_schema())

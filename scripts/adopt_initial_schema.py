"""Validate an existing database before adopting the consolidated initial revision.

Run without --apply to inspect compatibility. --apply changes only Alembic's
revision marker; it never creates, drops or rewrites business tables.
"""

import argparse
import asyncio

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, MetaData, text

import app.bootstrap.model_registry  # noqa: F401
from app.bootstrap.config import get_settings
from app.modules.operational_graph.application.change_capture import SOURCE_TABLES
from app.shared.db import Base, Database


async def adopt(*, apply=False):
    database = Database(get_settings().database_url)
    try:
        async with database.engine.begin() as connection:

            def compare(sync_connection):
                context = MigrationContext.configure(sync_connection, opts={"compare_type": True})
                baseline = MetaData(naming_convention=Base.metadata.naming_convention)
                for table in Base.metadata.sorted_tables:
                    if table.name not in {"prompt_optimizations", "station_locations"}:
                        table.to_metadata(baseline)
                jobs = baseline.tables["async_jobs"]
                for constraint in jobs.constraints:
                    if isinstance(constraint, CheckConstraint) and "async_job_status_valid" in str(
                        constraint.name
                    ):
                        constraint.sqltext = text(
                            "status IN ('queued','running','succeeded','failed','cancelled')"
                        )
                for index in jobs.indexes:
                    if index.name == "uq_async_jobs_active_resource":
                        index.dialect_options["postgresql"]["where"] = text(
                            "status IN ('queued','running')"
                        )
                return compare_metadata(context, baseline)

            differences = await connection.run_sync(compare)
            if differences:
                raise ValueError(
                    f"database schema differs from the initial revision: {differences}"
                )
            triggers = set(
                await connection.scalars(
                    text(
                        "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE t.tgname='business_graph_change' AND n.nspname='public'"
                    )
                )
            )
            if not set(SOURCE_TABLES) <= triggers:
                raise ValueError("business graph change triggers are missing")
            revisions = list(
                await connection.scalars(text("SELECT version_num FROM alembic_version FOR UPDATE"))
            )
            if revisions not in [["20261110_0007"], ["0001"]]:
                raise ValueError(f"unsupported existing revision: {revisions}")
            if apply and revisions != ["0001"]:
                await connection.execute(
                    text(
                        "UPDATE alembic_version SET version_num='0001' "
                        "WHERE version_num='20261110_0007'"
                    )
                )
            return {
                "compatible": True,
                "previous_revision": revisions[0],
                "revision": "0001" if apply else revisions[0],
                "applied": apply,
            }
    finally:
        await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    print(asyncio.run(adopt(apply=parser.parse_args().apply)))

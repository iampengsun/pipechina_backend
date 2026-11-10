"""Publish a new graph generation only after its complete snapshot is written successfully."""

import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert

from app.modules.operational_graph.application.catalog import build_catalog
from app.modules.operational_graph.application.impacts import materialize_reviews
from app.modules.operational_graph.domain.models import GraphChange, GraphProjection


async def synchronize(factory, graph, *, force=False):
    try:
        async with factory() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(716024918)")):
                return {"status": "busy"}
            await session.execute(
                insert(GraphProjection)
                .values(id=1, generation="", counts={})
                .on_conflict_do_nothing()
            )
            state = await session.get(GraphProjection, 1, with_for_update=True)
            changes = list(await session.scalars(select(GraphChange.id)))
            if state.generation and not changes and not force:
                # Time expiry also changes impact obligations without a new source transaction.
                catalog = await build_catalog(session)
                count = await materialize_reviews(session, catalog, graph, state.generation)
                state.last_error = None
                await session.commit()
                return {
                    "status": "up_to_date",
                    "generation": state.generation,
                    "new_reviews": count,
                }
            catalog = await build_catalog(session)
            generation = uuid4().hex
            await graph.replace(
                generation, [r.node for r in catalog.records.values()], list(catalog.edges.values())
            )
            reviews = await materialize_reviews(session, catalog, graph, generation)
            state.generation = generation
            state.synced_at = datetime.now(UTC)
            state.last_error = None
            state.counts = {
                "nodes": len(catalog.records),
                "edges": len(catalog.edges),
                "processed_changes": len(changes),
                "new_reviews": reviews,
            }
            # Consume exact visible events: sequence values do not imply commit order.
            for start in range(0, len(changes), 1000):
                await session.execute(
                    delete(GraphChange).where(GraphChange.id.in_(changes[start : start + 1000]))
                )
            await session.commit()
            result = {"status": "synchronized", "generation": generation, **state.counts}
        try:
            await graph.delete_other_generations(generation)
        except Exception:
            logging.getLogger(__name__).exception(
                "old graph cleanup failed; published generation remains valid"
            )
        return result
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "business graph projection failed; source changes retained"
        )
        async with factory() as session:
            await session.execute(
                insert(GraphProjection)
                .values(id=1, generation="", counts={})
                .on_conflict_do_nothing()
            )
            state = await session.get(GraphProjection, 1)
            state.last_error = type(exc).__name__
            await session.commit()
        raise

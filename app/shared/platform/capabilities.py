"""Immutable multi-capability model snapshots and metadata-only call observations."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.errors import ConflictError
from app.shared.platform.models import AsyncJob, ModelAlias
from app.shared.platform.schemas import ModelAliasInput
from app.shared.platform.service import add_ai_call_log


async def capability_snapshot(
    session: AsyncSession,
    aliases: dict[str, str],
    *,
    strategy_versions: dict[str, str],
    generation: str,
    defaults: dict[str, dict] | None = None,
) -> dict:
    """No credentials/URLs in snapshots. Missing or mismatched aliases fail closed."""
    models = {}
    for capability, code in aliases.items():
        alias = await session.scalar(select(ModelAlias).where(ModelAlias.code == code))
        if alias is None:
            if defaults is None or capability not in defaults:
                raise ConflictError("missing model alias", alias=code)
            validated = ModelAliasInput(capability=capability, **defaults[capability])
            models[capability] = {"alias": code, **validated.model_dump()}
            continue
        if not alias.enabled or alias.capability != capability:
            raise ConflictError("missing, disabled or incompatible model alias", alias=code)
        validated = ModelAliasInput(
            provider=alias.provider,
            model_name=alias.model_name,
            capability=capability,
            model_snapshot=alias.model_snapshot,
            config={**(defaults or {}).get(capability, {}).get("config", {}), **alias.config},
        )
        models[capability] = {
            "alias": code,
            **validated.model_dump(),
            "updated_at": alias.updated_at.isoformat(),
        }
    return {
        "capabilities": models,
        "strategy_versions": dict(strategy_versions),
        "index_generation": generation,
    }


@asynccontextmanager
async def observe_capability(
    session: AsyncSession,
    *,
    job: AsyncJob,
    run_id: UUID,
    capability: str,
    provider: str,
    model: str,
    alias: str,
):
    """Caller supplies measured units; never infer token counts from character counts."""
    started = datetime.now(UTC)
    units: dict[str, int | None] = {"input_units": None, "output_units": None}
    status, error = "succeeded", None
    try:
        yield units
    except BaseException as exc:
        status = "timeout" if isinstance(exc, TimeoutError) else "failed"
        error = type(exc).__name__
        raise
    finally:
        await add_ai_call_log(
            session,
            job=job,
            run_id=run_id,
            capability=capability,
            provider=provider,
            model_alias=alias,
            model_name=model,
            started_at=started,
            status=status,
            error_code=error,
            input_units=units["input_units"],
            output_units=units["output_units"],
        )

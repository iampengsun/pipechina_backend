"""One durable budget for every role and sub-investigation in an AgentRun."""

from app.shared.errors import ConflictError
from app.shared.platform.execution import guard_execution


async def reserve(session, run, job, token, kind):
    await session.flush()
    await guard_execution(session, job.id, token)
    await session.refresh(run, with_for_update={"key_share": True})
    limits = run.config_snapshot.get("limits", {})
    usage = dict(run.result.get("budget", {}))
    field = kind + "_calls"
    default = 40 if kind == "model" else 20
    if usage.get(field, 0) >= limits.get("max_" + field, default):
        raise ConflictError("investigation call budget exhausted", kind=kind)
    if run.input_tokens + run.output_tokens >= limits.get("max_tokens", 60000):
        raise ConflictError("investigation token budget exhausted")
    cost_limit = limits.get("max_cost_microusd")
    if cost_limit is not None and run.cost_microusd >= cost_limit:
        raise ConflictError("investigation cost budget exhausted")
    usage[field] = usage.get(field, 0) + 1
    run.result = {**run.result, "budget": usage}
    await session.commit()


def check_usage(run):
    limits = run.config_snapshot.get("limits", {})
    if run.input_tokens + run.output_tokens > limits.get("max_tokens", 60000):
        raise ConflictError("investigation token budget exceeded")
    limit = limits.get("max_cost_microusd")
    if limit is not None and run.cost_microusd > limit:
        raise ConflictError("investigation cost budget exceeded")


def bounded_provider(provider, run, system, payload, model):
    """Reserve conservative input bytes and cap provider output before spending."""
    from copy import copy

    configured = copy(provider)
    target = copy(provider.provider) if hasattr(provider, "provider") else configured
    if hasattr(provider, "provider"):
        configured.provider = target
    limit = run.config_snapshot.get("limits", {})
    # UTF-8 bytes conservatively cover text tokens, schema and request framing.
    input_bound = request_size(system, payload, model)
    budget = getattr(run, "result", {}).get("budget", {})
    pending = list(budget.get("reservations", {}).values())
    remaining = (
        limit.get("max_tokens", 60000)
        - run.input_tokens
        - run.output_tokens
        - budget.get("estimated_tokens", 0)
        - sum(r["tokens"] for r in pending)
    )
    output = min(int(getattr(target, "max_tokens", None) or 4096), remaining - input_bound)
    cost = limit.get("max_cost_microusd")
    if cost is not None:
        pricing = run.config_snapshot["pricing_microusd_per_million"]
        remaining_cost = (
            cost
            - run.cost_microusd
            - budget.get("estimated_cost", 0)
            - sum(r["cost"] for r in pending)
        )
        output = min(
            output, (remaining_cost * 1000000 - input_bound * pricing["input"]) // pricing["output"]
        )
    if output < 128:
        raise ConflictError("remaining investigation budget cannot cover this request")
    target.max_tokens = output
    return configured


def request_size(system, payload, model):
    import json

    return len((system + payload + json.dumps(model.model_json_schema())).encode()) + 1024


async def reserve_request(session, run, job, token, provider, system, payload, model, identifier):
    """Hold a request's worst-case tokens/cost across concurrent RAG branches."""
    await guard_execution(session, job.id, token)
    await session.refresh(run, with_for_update={"key_share": True})
    configured = bounded_provider(provider, run, system, payload, model)
    target = configured.provider if hasattr(configured, "provider") else configured
    inputs, outputs = request_size(system, payload, model), target.max_tokens
    prices = run.config_snapshot.get("pricing_microusd_per_million", {})
    cost = (inputs * prices.get("input", 0) + outputs * prices.get("output", 0) + 999999) // 1000000
    budget = dict(run.result.get("budget", {}))
    held = dict(budget.get("reservations", {}))
    held[str(identifier)] = {"tokens": inputs + outputs, "cost": cost}
    budget["reservations"] = held
    run.result = {**run.result, "budget": budget}
    await session.commit()
    return configured


def settle_request(run, identifier, *, usage_missing=False):
    budget = dict(run.result.get("budget", {}))
    held = dict(budget.get("reservations", {}))
    reservation = held.pop(str(identifier), {})
    if usage_missing:
        budget["estimated_tokens"] = budget.get("estimated_tokens", 0) + reservation.get(
            "tokens", 0
        )
        budget["estimated_cost"] = budget.get("estimated_cost", 0) + reservation.get("cost", 0)
    budget["reservations"] = held
    run.result = {**run.result, "budget": budget}

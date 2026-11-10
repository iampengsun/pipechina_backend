from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.shared.errors import AppError
from app.shared.platform.resilience import CircuitBreaker


@pytest.mark.asyncio
async def test_breaker_blocks_failure_bursts_and_allows_one_recovery_probe():
    breaker = CircuitBreaker(threshold=2, cooldown=30)
    await breaker.enter()
    await breaker.finish(True)
    await breaker.enter()
    await breaker.finish(True)
    with pytest.raises(AppError, match="DEPENDENCY_CIRCUIT_OPEN"):
        await breaker.enter()
    breaker.blocked_until = 0
    await breaker.enter()
    with pytest.raises(AppError, match="DEPENDENCY_CIRCUIT_OPEN"):
        await breaker.enter()
    await breaker.abort()
    assert breaker.failures == 2
    await breaker.enter()
    await breaker.finish(False)
    await breaker.enter()
    assert breaker.failures == 0


def test_shared_request_gate_and_protected_metrics(client):
    client.app.state.settings.app_env = "development"
    client.app.state.settings.request_rate_limit_per_minute = 1
    client.app.state.settings.ops_metrics_token = SecretStr("synthetic-metrics-test-token")
    redis = AsyncMock()
    redis.eval.side_effect = [1, 2, RuntimeError("redis unavailable")]
    client.app.state.rate_redis = redis
    assert client.get("/api/v1/not-a-route").status_code == 404
    limited = client.get("/api/v1/not-a-route")
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    assert client.get("/api/v1/not-a-route").status_code == 503
    assert client.get("/health/live").status_code == 200
    assert client.get("/metrics").status_code == 401
    metrics = client.get(
        "/metrics", headers={"Authorization": "Bearer synthetic-metrics-test-token"}
    )
    assert metrics.status_code == 200
    assert "pipechina_runtime_disk_available_bytes " in metrics.text
    assert "pipechina_runtime_disk_total_bytes " in metrics.text
    assert 'status="429"' in metrics.text
    assert 'status="503"' in metrics.text

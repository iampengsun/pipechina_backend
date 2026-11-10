"""Shared Redis request limiting and bounded process metrics for Prometheus."""

import hashlib
import secrets
import shutil
from collections import Counter
from datetime import UTC, datetime
from time import monotonic, time

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.shared.platform.models import AsyncJob, TaskOutbox

RATE_SCRIPT = """
local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],61) end; return n
"""


def install_operations(app, settings):
    counts: Counter[int] = Counter()
    durations = {bound: 0 for bound in [0.1, 0.5, 1.0, 5.0, 30.0, float("inf")]}
    elapsed_total = 0.0

    app.state.rate_redis = Redis.from_url(settings.celery_result_backend, socket_timeout=2)

    async def request_gate(request):
        if not request.url.path.startswith(("/api/", "/mcp")) or settings.app_env == "test":
            return None
        address = request.client.host if request.client else "unknown"
        key = (
            "pipechina:rate:"
            + hashlib.sha256(address.encode()).hexdigest()
            + f":{int(time() // 60)}"
        )
        try:
            number = await app.state.rate_redis.eval(RATE_SCRIPT, 1, key)
        except Exception:
            return JSONResponse(
                {"code": "RATE_LIMIT_UNAVAILABLE", "message": "request gate unavailable"}, 503
            )
        if number > settings.request_rate_limit_per_minute:
            return JSONResponse(
                {"code": "RATE_LIMIT_EXCEEDED", "message": "too many requests"},
                429,
                headers={"Retry-After": str(60 - int(time()) % 60)},
            )
        return None

    @app.middleware("http")
    async def observe(request: Request, call_next):
        nonlocal elapsed_total
        start = monotonic()
        status = 500
        try:
            response = await request_gate(request)
            if response is None:
                response = await call_next(request)
            status = response.status_code
            return response
        finally:
            if request.url.path != "/metrics":
                elapsed = monotonic() - start
                elapsed_total += elapsed
                counts[status] += 1
                for bound in durations:
                    durations[bound] += int(elapsed <= bound)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        expected = settings.ops_metrics_token
        if not expected or not secrets.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + expected.get_secret_value()
        ):
            return JSONResponse({"code": "METRICS_UNAUTHORIZED"}, 401)
        lines = ["# TYPE pipechina_http_requests_total counter"]
        lines.extend(
            f'pipechina_http_requests_total{{status="{code}"}} {value}'
            for code, value in sorted(counts.items())
        )
        lines.append("# TYPE pipechina_http_duration_seconds histogram")
        for bound, count in durations.items():
            label = "+Inf" if bound == float("inf") else str(bound)
            lines.append(f'pipechina_http_duration_seconds_bucket{{le="{label}"}} {count}')
        lines.extend(
            [
                f"pipechina_http_duration_seconds_sum {elapsed_total}",
                f"pipechina_http_duration_seconds_count {sum(counts.values())}",
            ]
        )
        disk = shutil.disk_usage("/")
        lines.extend(
            [
                "# TYPE pipechina_runtime_disk_available_bytes gauge",
                f"pipechina_runtime_disk_available_bytes {disk.free}",
                "# TYPE pipechina_runtime_disk_total_bytes gauge",
                f"pipechina_runtime_disk_total_bytes {disk.total}",
            ]
        )
        async with app.state.database.session_factory() as session:
            for status_column, name in [
                (AsyncJob.status, "pipechina_jobs"),
                (TaskOutbox.status, "pipechina_outbox"),
            ]:
                rows = await session.execute(
                    select(status_column, func.count()).group_by(status_column)
                )
                lines.append("# TYPE " + name + " gauge")
                lines.extend(f'{name}{{status="{status}"}} {count}' for status, count in rows)
            oldest = await session.scalar(
                select(func.min(AsyncJob.created_at)).where(AsyncJob.status == "queued")
            )
            age = max(0, (datetime.now(UTC) - oldest).total_seconds()) if oldest else 0
            lines.append("# TYPE pipechina_oldest_queued_seconds gauge")
            lines.append(f"pipechina_oldest_queued_seconds {age}")
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

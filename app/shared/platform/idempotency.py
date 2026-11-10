"""HTTP写请求幂等中间件。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.shared.db import Database
from app.shared.errors import AppError
from app.shared.platform.models import IdempotencyRecord
from app.shared.security.authentication.dependencies import _verify_token
from app.shared.security.identity.repository import resolve_identity

# 只处理带Idempotency-Key的JSON写请求；查询和文件流不进入幂等缓存。


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """对携带Idempotency-Key的JSON写请求进行预约、冲突检测和响应重放。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """预约幂等键并根据首次请求结果决定放行或冲突。"""

        # Knowledge responses depend on live document ACL/publication state. Do not replay
        # them before endpoint authorization; lifecycle jobs provide resource-level deduplication.
        settings = getattr(request.app.state, "settings", None)
        if settings and (
            request.url.path.startswith(settings.api_prefix + "/knowledge/")
            or request.url.path.startswith("/mcp")
        ):
            return await call_next(request)
        key = request.headers.get("Idempotency-Key")
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or not key:
            return await call_next(request)
        if len(key) > 200:
            return _error(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key is too long")
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return await call_next(request)
        try:
            token = await _verify_token(
                request,
                HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=authorization.removeprefix("Bearer ").strip()
                ),
            )
        except AppError as exc:
            return _error(exc.status_code, exc.code, exc.message)

        body = await request.body()
        operation = f"{request.method} {request.url.path}"
        digest = hashlib.sha256(
            request.method.encode()
            + b"\0"
            + request.url.path.encode()
            + b"\0"
            + request.url.query.encode()
            + b"\0"
            + body
        ).hexdigest()
        database = request.app.state.database
        record: IdempotencyRecord | None = None
        async with database.session_factory() as session:
            try:
                identity = await resolve_identity(session, token)
            except AppError as exc:
                return _error(exc.status_code, exc.code, exc.message)
            actor_id = identity.user_id
            record = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.actor_id == actor_id,
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.idempotency_key == key,
                )
            )
            if record is not None:
                replay = _replay_or_conflict(record, digest)
                if replay is not None:
                    return replay
            else:
                record = IdempotencyRecord(
                    actor_id=actor_id,
                    operation=operation,
                    idempotency_key=key,
                    request_digest=digest,
                )
                session.add(record)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    record = await session.scalar(
                        select(IdempotencyRecord).where(
                            IdempotencyRecord.actor_id == actor_id,
                            IdempotencyRecord.operation == operation,
                            IdempotencyRecord.idempotency_key == key,
                        )
                    )
                    assert record is not None
                    replay = _replay_or_conflict(record, digest)
                    if replay is not None:
                        return replay

        assert record is not None
        try:
            response = await call_next(request)
            if "application/json" not in response.headers.get("content-type", ""):
                await _delete_reservation(database, record.id)
                return response
            response_body = b"".join(
                [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
            )
        except Exception:
            await _delete_reservation(database, record.id)
            raise

        content_type = response.headers.get("content-type", "")
        cacheable = (
            200 <= response.status_code < 300
            and "application/json" in content_type
            and len(response_body) <= 1_000_000
        )
        if cacheable:
            try:
                json.loads(response_body)
            except TypeError, ValueError:
                cacheable = False
        if cacheable:
            async with database.session_factory() as session:
                saved = await session.get(IdempotencyRecord, record.id)
                if saved is not None:
                    saved.response_status = response.status_code
                    saved.response_body = {"completed": True}
                    await session.commit()
        else:
            await _delete_reservation(database, record.id)

        headers = dict(response.headers)
        headers["Idempotency-Key"] = key
        return Response(
            content=response_body,
            status_code=response.status_code,
            headers=headers,
            media_type=None,
            background=response.background,
        )


def _replay_or_conflict(record: IdempotencyRecord, digest: str) -> Response | None:
    """比较请求摘要并返回对应的幂等冲突响应。"""

    if record.request_digest != digest:
        return _error(
            409,
            "IDEMPOTENCY_KEY_REUSED",
            "the same Idempotency-Key was used with a different request",
        )
    if record.response_status is None or record.response_body is None:
        updated_at = record.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if updated_at <= datetime.now(UTC) - timedelta(minutes=5):
            return _error(
                409,
                "REQUEST_OUTCOME_UNKNOWN",
                "request outcome is unknown; reconcile before retrying",
            )
        return _error(409, "REQUEST_IN_PROGRESS", "an identical request is still in progress")
    # Middleware cannot re-authorize a resource whose ownership/assignment may have
    # changed. Keep duplicate suppression, but never replay a business payload here.
    # Clients must read the resource through its normally authorized GET endpoint.
    return _error(
        409, "REQUEST_ALREADY_COMPLETED", "request already completed; reload the resource"
    )


async def _delete_reservation(database: Database, record_id: UUID) -> None:
    """删除未形成可缓存结果的幂等预约。"""

    async with database.session_factory() as session:
        row = await session.get(IdempotencyRecord, record_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


def _error(status: int, code: str, message: str) -> JSONResponse:
    """生成幂等中间件使用的简单错误响应。"""

    return JSONResponse(status_code=status, content={"code": code, "message": message})

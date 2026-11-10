from __future__ import annotations

# FastAPI应用入口：装配数据库、Provider、中间件、异常处理器和业务路由。
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from sqlalchemy import text

from app import __version__
from app.bootstrap.config import Settings, get_settings
from app.bootstrap.job_lifecycle import (
    retry_handlers,
    synchronize_cancelled_job,
    validate_prompt_activation,
)
from app.bootstrap.knowledge import KnowledgeResourceManager
from app.bootstrap.providers import create_provider_bundle
from app.bootstrap.site_context import create_site_context
from app.modules.handover.api.router import router as handover_router
from app.modules.hazard.api.attachments_router import router as hazard_attachments_router
from app.modules.hazard.api.router import router as hazard_router
from app.modules.inspection.api.router import router as inspection_router
from app.modules.knowledge.api.router import router as knowledge_router
from app.modules.maintenance_order.api.outcomes_router import router as outcomes_router
from app.modules.maintenance_order.api.router import router as maintenance_router
from app.modules.meeting.api.router import router as meeting_router
from app.modules.operation_event.api.mcp_server import build_mcp_server
from app.modules.operation_event.api.memory_router import router as memory_router
from app.modules.operation_event.api.router import router as event_router
from app.modules.operational_graph.api.router import router as operational_graph_router
from app.modules.quality.api.router import router as quality_router
from app.modules.report.api.router import router as report_router
from app.shared.db import Database
from app.shared.errors import install_error_handlers
from app.shared.media.router import router as storage_transfer_router
from app.shared.platform.idempotency import IdempotencyMiddleware
from app.shared.platform.operations import install_operations
from app.shared.platform.router import admin_router, jobs_router
from app.shared.security.audit.context import request_id_context, reset_request_id
from app.shared.security.authorization.repository import sync_builtin_roles
from app.shared.security.authorization.router import router as access_control_router
from app.shared.security.router import router as auth_router


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()
    mcp_server: MCPServer

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = configured
        app.state.database = Database(
            configured.database_url,
            use_null_pool=configured.app_env == "test",
        )
        app.state.providers = create_provider_bundle(configured)
        app.state.knowledge = KnowledgeResourceManager(configured)
        app.state.site_context = create_site_context(configured)
        app.state.job_retry_handlers = retry_handlers()
        app.state.validate_prompt_activation = validate_prompt_activation
        app.state.synchronize_cancelled_job = synchronize_cancelled_job
        if configured.auto_create_schema:
            await app.state.database.create_all()
        if configured.auto_seed:
            async with app.state.database.session_factory() as session:
                await sync_builtin_roles(session)
                await session.commit()
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            try:
                try:
                    await app.state.site_context.close()
                finally:
                    await app.state.knowledge.close()
            finally:
                try:
                    await app.state.providers.close()
                finally:
                    await app.state.database.dispose()
                    await app.state.rate_redis.aclose()

    app = FastAPI(
        title=configured.app_name,
        version=__version__,
        description="模块化单体后端；AI 结果默认是候选，正式业务动作需要确定性校验与人工授权。",
        lifespan=lifespan,
    )
    mcp_server, mcp_app = build_mcp_server(
        configured,
        lambda: app.state.database,
        lambda: app.state.knowledge,
        lambda: app.state.providers.text,
        lambda: app.state.site_context,
    )
    app.mount("/mcp", mcp_app)
    install_operations(app, configured)
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "Idempotency-Key",
            "Mcp-Protocol-Version",
            "Mcp-Method",
            "Mcp-Name",
            "Mcp-Session-Id",
        ],
        expose_headers=["X-Request-ID", "Idempotency-Key", "Idempotency-Replayed"],
        max_age=600,
    )
    app.add_middleware(IdempotencyMiddleware)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
        token = request_id_context(request.state.request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        finally:
            reset_request_id(token)

    @app.get("/health/live", tags=["health"])
    async def health_live() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    async def health_ready(request: Request):
        try:
            async with request.app.state.database.session_factory() as session:
                await session.execute(text("SELECT 1"))
            checks = await request.app.state.knowledge.get().health()
            if not all(checks.values()):
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "knowledge": checks},
                )
            return {"status": "ready"}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})

    @app.get("/health/knowledge", tags=["health"])
    async def health_knowledge(request: Request):
        checks = await request.app.state.knowledge.get().health()
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "components": checks,
                "retrieval_ready": all(
                    checks[n] for n in ("fulltext", "vector", "graph", "embedding", "reranker")
                ),
                "processing_ready": all(
                    checks[n] for n in ("parser", "embedding", "fulltext", "vector", "graph")
                ),
            },
        )

    for router in (
        auth_router,
        access_control_router,
        jobs_router,
        admin_router,
        storage_transfer_router,
        handover_router,
        hazard_router,
        hazard_attachments_router,
        meeting_router,
        event_router,
        memory_router,
        maintenance_router,
        outcomes_router,
        inspection_router,
        report_router,
        knowledge_router,
        quality_router,
        operational_graph_router,
    ):
        app.include_router(router, prefix=configured.api_prefix)

    return app


app = create_app()

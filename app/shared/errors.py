from dataclasses import dataclass

# 定义统一业务异常及FastAPI异常响应，避免各路由重复拼装错误格式。
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorBody(BaseModel):
    """定义所有业务异常的统一响应结构。"""

    code: str
    message: str
    request_id: str | None = None
    details: dict[str, Any] | None = None


@dataclass(slots=True)
class AppError(Exception):
    """携带稳定错误码和HTTP状态的业务异常基类。"""

    code: str
    message: str
    status_code: int = 400
    details: dict[str, Any] | None = None


class NotFoundError(AppError):
    """表示指定业务资源不存在。"""

    def __init__(self, resource: str, resource_id: object) -> None:
        """保存未找到资源的类型和编号。"""

        super().__init__(
            code="RESOURCE_NOT_FOUND",
            message=f"{resource} not found",
            status_code=404,
            details={"id": str(resource_id)},
        )


class ConflictError(AppError):
    """表示请求与资源当前状态发生冲突。"""

    def __init__(self, message: str, **details: Any) -> None:
        """保存状态冲突说明和附加信息。"""

        super().__init__("STATE_CONFLICT", message, 409, details or None)


class PermissionDeniedError(AppError):
    """表示当前用户缺少指定操作权限。"""

    def __init__(self, permission: str) -> None:
        """保存本次操作缺失的权限代码。"""

        super().__init__(
            "PERMISSION_DENIED",
            "current user does not have the required permission",
            403,
            {"permission": permission},
        )


def install_error_handlers(app: FastAPI) -> None:
    """给FastAPI安装统一业务异常处理器。"""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """把业务异常转换为统一JSON响应。"""

        body = ErrorBody(
            code=exc.code,
            message=exc.message,
            request_id=getattr(request.state, "request_id", None),
            details=exc.details,
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

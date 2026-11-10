from contextvars import ContextVar, Token

# 使用ContextVar让同一异步请求中的审计记录共享request_id。
_request_id: ContextVar[str | None] = ContextVar("security_request_id", default=None)


def request_id_context(value: str) -> Token[str | None]:
    """写入当前请求标识，并返回用于恢复上下文的令牌。"""
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    """使用上下文令牌恢复先前的请求标识。"""
    _request_id.reset(token)


def current_request_id() -> str | None:
    """获取当前执行上下文中的请求标识。"""
    return _request_id.get()

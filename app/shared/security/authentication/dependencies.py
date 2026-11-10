from __future__ import annotations

# FastAPI认证依赖：校验JWT签名、标准声明和IdP身份映射。
import asyncio
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from app.bootstrap.config import Settings
from app.shared.db import SessionDep
from app.shared.errors import AppError
from app.shared.security.authentication.schemas import AuthenticatedIdentity, VerifiedToken
from app.shared.security.identity.repository import resolve_identity

bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=16)
def _get_jwk_client(url: str, cache_seconds: int, timeout_seconds: float) -> PyJWKClient:
    """创建并缓存用于获取 JWT 的客户端。"""
    return PyJWKClient(
        url,
        cache_keys=True,
        lifespan=cache_seconds,
        timeout=timeout_seconds,
    )


async def _verify_token(  # 验证token对不对，对就将其解析出来，错就抛出异常
    request: Request,  # request表示用户发出的请求
    credentials: HTTPAuthorizationCredentials
    | None,  # 本项目中将这个参数想象成为是传来的token就可以了
) -> VerifiedToken:
    """校验 bearer token，并将声明转换为已验证令牌。"""
    if credentials is None:
        raise AppError("AUTHENTICATION_REQUIRED", "bearer token is required", 401)
    return await verify_bearer_token(request.app.state.settings, credentials.credentials)


async def verify_bearer_token(settings: Settings, raw_token: str) -> VerifiedToken:
    """供 HTTP API 与 MCP 共用同一套 JWT 校验。"""
    try:
        signing_key = await asyncio.to_thread(
            _get_jwk_client(
                settings.jwt_jwks_url,
                settings.jwt_jwks_cache_seconds,
                settings.jwt_jwks_timeout_seconds,
            ).get_signing_key_from_jwt,
            raw_token,
        )  # 在线程中获取匹配的签名密钥

        claims: dict[str, Any] = jwt.decode(  # decode就是用刚拿到的公钥对token解码
            raw_token,
            signing_key.key,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )  # 校验签名及必需声明
        # claims就相当于被还原回来的token内容
        issuer = str(claims["iss"])
        subject = str(claims["sub"])
        org_claim = claims.get("org")
        org_id = UUID(str(org_claim)) if org_claim is not None else None
        return VerifiedToken(
            issuer=issuer,
            subject=subject,
            username=str(claims.get("preferred_username", subject)),
            display_name=str(claims.get("name", subject)),
            organization_unit_id=org_id,
            claims=claims,
        )
    except (
        InvalidTokenError,
        PyJWKClientError,
        KeyError,
        ValueError,
        TypeError,
    ) as exc:
        raise AppError(
            "INVALID_TOKEN",
            "bearer token is invalid or expired",
            401,
        ) from exc


async def get_authenticated_identity(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthenticatedIdentity:
    """验证请求令牌并解析对应的内部用户身份。"""
    token = await _verify_token(request, credentials)  # 先完成令牌校验
    return await resolve_identity(session, token)


AuthenticatedIdentityDep = Annotated[
    AuthenticatedIdentity,
    Depends(get_authenticated_identity),
]

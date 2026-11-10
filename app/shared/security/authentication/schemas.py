from typing import Any

# 认证阶段使用的数据模型，区分已验证令牌和项目内部登录身份。
from uuid import UUID

from pydantic import BaseModel, Field


class VerifiedToken(BaseModel):
    """保存通过校验的令牌声明和身份信息。"""

    issuer: str
    subject: str
    username: str
    display_name: str
    organization_unit_id: UUID | None = None
    claims: dict[str, Any] = Field(default_factory=dict, exclude=True)


class AuthenticatedIdentity(BaseModel):
    """表示已映射到系统用户的认证身份。"""

    user_id: UUID
    issuer: str
    subject: str
    username: str
    display_name: str
    organization_unit_id: UUID
    authz_version: int

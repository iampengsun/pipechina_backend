from __future__ import annotations

# 将已验证的IdP令牌解析为本地用户，并拒绝未配置或已停用账号。
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.errors import AppError
from app.shared.security.authentication.schemas import AuthenticatedIdentity, VerifiedToken
from app.shared.security.identity.models import OrganizationUnit, UserAccount


async def resolve_identity(
    session: AsyncSession,
    token: VerifiedToken,
) -> AuthenticatedIdentity:
    """根据外部令牌查找并校验内部用户身份。"""
    user = await session.scalar(
        select(UserAccount).where(
            UserAccount.external_issuer == token.issuer,
            UserAccount.external_subject == token.subject,
        )
    )

    if user is None:
        raise AppError(
            "ACCOUNT_NOT_PROVISIONED",
            "the authenticated identity is not provisioned",
            403,
        )

    if not user.active:
        raise AppError("ACCOUNT_DISABLED", "the user account is disabled", 403)

    organization = await session.get(OrganizationUnit, user.organization_unit_id)  # 校验所属组织
    if organization is None or not organization.active:
        raise AppError("ORGANIZATION_DISABLED", "the user organization is unavailable", 403)

    return AuthenticatedIdentity(
        user_id=user.id,
        issuer=user.external_issuer,
        subject=user.external_subject,
        username=user.username,
        display_name=user.display_name,
        organization_unit_id=user.organization_unit_id,
        authz_version=user.authz_version,
    )

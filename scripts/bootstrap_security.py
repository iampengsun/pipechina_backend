"""初始化正式外部 IdP 对应的首个数据库系统管理员。

用途：创建或复用组织和用户，并授予全局 system_administrator 角色。
时机：接入正式 IdP 后首次建立管理员；Mock IdP 开发环境使用 bootstrap_mock_idp_users.py。
前置：`.env` 指向目标数据库，且已执行 `uv run alembic upgrade head`。
运行：
  uv run python scripts/bootstrap_security.py \
    --issuer https://idp.example.com --subject USER_SUB --username admin \
    --display-name "管理员" --organization-code ROOT --organization-name "总部"
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID, uuid4

from sqlalchemy import select

from app.bootstrap.config import get_settings
from app.shared.db import Database
from app.shared.platform.service import scope_digest
from app.shared.security.authorization.models import RoleAssignment
from app.shared.security.authorization.repository import sync_builtin_roles
from app.shared.security.authorization.scopes import ScopeType
from app.shared.security.identity.models import OrganizationUnit, UserAccount


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--organization-code", required=True)
    parser.add_argument("--organization-name", required=True)
    parser.add_argument("--organization-id", type=UUID, default=None)
    return parser.parse_args()


async def _bootstrap(args: argparse.Namespace) -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session_factory() as session:
            roles = await sync_builtin_roles(session)
            organization = await session.scalar(
                select(OrganizationUnit).where(OrganizationUnit.code == args.organization_code)
            )
            if organization is None:
                organization = OrganizationUnit(
                    id=args.organization_id or uuid4(),
                    parent_id=None,
                    code=args.organization_code,
                    name=args.organization_name,
                    unit_type="company",
                    path=f"/{args.organization_code}",
                    active=True,
                )
                session.add(organization)
                await session.flush()
            user = await session.scalar(
                select(UserAccount).where(
                    UserAccount.external_issuer == args.issuer,
                    UserAccount.external_subject == args.subject,
                )
            )
            if user is None:
                user = UserAccount(
                    external_issuer=args.issuer,
                    external_subject=args.subject,
                    username=args.username,
                    display_name=args.display_name,
                    organization_unit_id=organization.id,
                    active=True,
                    authz_version=1,
                    attributes={"provisioning": "security_bootstrap"},
                )
                session.add(user)
                await session.flush()
            role = roles["system_administrator"]
            existing = await session.scalar(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user.id,
                    RoleAssignment.role_id == role.id,
                    RoleAssignment.active.is_(True),
                )
            )
            if existing is None:
                document = {"type": ScopeType.GLOBAL.value, "organization_unit_ids": []}
                session.add(
                    RoleAssignment(
                        user_id=user.id,
                        role_id=role.id,
                        scope_type=ScopeType.GLOBAL.value,
                        data_scope=document,
                        scope_digest=scope_digest(document),
                        active=True,
                        assigned_by=user.id,
                        grant_reason="initial security bootstrap",
                    )
                )
            await session.commit()
            print(f"Provisioned administrator user_id={user.id} organization_id={organization.id}")
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(_bootstrap(_arguments()))

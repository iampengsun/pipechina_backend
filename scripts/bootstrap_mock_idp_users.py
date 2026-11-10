"""初始化本地 Mock IdP 开发用户和内置角色。

用途：创建或更新 admin、peter、tom 及其开发权限；可重复执行。
时机：首次搭建开发环境、重建数据库或修改 Mock IdP 身份配置后。
前置：`.env` 指向开发数据库，且已执行 `uv run alembic upgrade head`。
运行：`uv run python scripts/bootstrap_mock_idp_users.py`
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import or_, select

from app.bootstrap.config import get_settings
from app.shared.db import Database
from app.shared.platform.service import scope_digest
from app.shared.security.authorization.models import RoleAssignment
from app.shared.security.authorization.repository import sync_builtin_roles
from app.shared.security.authorization.scopes import ScopeType
from app.shared.security.identity.models import OrganizationUnit, UserAccount


@dataclass(frozen=True, slots=True)
class DevelopmentUser:
    username: str
    subject: str
    display_name: str
    active: bool
    role_code: str | None
    scope_type: ScopeType | None


DEVELOPMENT_USERS = (
    DevelopmentUser(
        username="admin",
        subject="mock-admin",
        display_name="管理员",
        active=True,
        role_code="system_administrator",
        scope_type=ScopeType.GLOBAL,
    ),
    DevelopmentUser(
        username="peter",
        subject="mock-peter",
        display_name="Peter",
        active=True,
        role_code="dispatcher",
        scope_type=ScopeType.OWN_ORG,
    ),
    DevelopmentUser(
        username="acceptance",
        subject="mock-acceptance",
        display_name="开发独立业务验收员",
        active=True,
        role_code="acceptance_reviewer",
        scope_type=ScopeType.OWN_ORG,
    ),
    DevelopmentUser(
        username="reviewer",
        subject="mock-reviewer",
        display_name="开发评测复核员",
        active=True,
        role_code="quality_reviewer",
        scope_type=ScopeType.OWN_ORG,
    ),
    DevelopmentUser(
        username="tom",
        subject="mock-tom",
        display_name="Tom",
        active=False,
        role_code=None,
        scope_type=None,
    ),
)


async def bootstrap() -> None:
    settings = get_settings()
    if settings.app_env == "production":
        raise RuntimeError("Mock IdP users cannot be provisioned in production")
    database = Database(settings.database_url)
    try:
        async with database.session_factory() as session:
            roles = await sync_builtin_roles(session)
            organization = await session.scalar(
                select(OrganizationUnit).where(OrganizationUnit.code == "ROOT")
            )
            if organization is None:
                organization = OrganizationUnit(
                    parent_id=None,
                    code="ROOT",
                    name="总部",
                    unit_type="company",
                    path="/ROOT",
                    active=True,
                )
                session.add(organization)
                await session.flush()

            provisioned: dict[str, UserAccount] = {}
            for specification in DEVELOPMENT_USERS:
                user = await session.scalar(
                    select(UserAccount).where(
                        or_(
                            (
                                (UserAccount.external_issuer == settings.jwt_issuer)
                                & (UserAccount.external_subject == specification.subject)
                            ),
                            UserAccount.username == specification.username,
                        )
                    )
                )
                if user is None:
                    user = UserAccount(
                        external_issuer=settings.jwt_issuer,
                        external_subject=specification.subject,
                        username=specification.username,
                        display_name=specification.display_name,
                        organization_unit_id=organization.id,
                        active=specification.active,
                        authz_version=1,
                        attributes={"provisioning": "mock_idp_bootstrap"},
                    )
                    session.add(user)
                    await session.flush()
                else:
                    user.external_issuer = settings.jwt_issuer
                    user.external_subject = specification.subject
                    user.display_name = specification.display_name
                    user.organization_unit_id = organization.id
                    user.active = specification.active
                    user.authz_version += 1
                provisioned[specification.username] = user

            admin = provisioned["admin"]
            for specification in DEVELOPMENT_USERS:
                if specification.role_code is None or specification.scope_type is None:
                    continue
                user = provisioned[specification.username]
                role = roles[specification.role_code]
                existing = await session.scalar(
                    select(RoleAssignment).where(
                        RoleAssignment.user_id == user.id,
                        RoleAssignment.role_id == role.id,
                        RoleAssignment.active.is_(True),
                    )
                )
                if existing is not None:
                    continue
                document: dict = {
                    "type": specification.scope_type.value,
                    "organization_unit_ids": [],
                }
                session.add(
                    RoleAssignment(
                        user_id=user.id,
                        role_id=role.id,
                        scope_type=specification.scope_type.value,
                        data_scope=document,
                        scope_digest=scope_digest(document),
                        active=True,
                        assigned_by=admin.id,
                        grant_reason="Mock IdP development identity bootstrap",
                    )
                )
            await session.commit()
            for username, user in provisioned.items():
                print(f"Provisioned {username}: user_id={user.id}, active={user.active}")
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(bootstrap())

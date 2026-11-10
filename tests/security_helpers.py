from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bootstrap.config import Settings
from app.main import create_app
from app.shared.platform.service import scope_digest
from app.shared.security.authorization.models import (
    RoleAssignment,
    RoleDefinition,
    RolePermission,
)
from app.shared.security.authorization.repository import sync_permission_catalog
from app.shared.security.authorization.scopes import ScopeType
from app.shared.security.identity.models import OrganizationUnit, UserAccount

TEST_ORG_ID = UUID("10000000-0000-0000-0000-000000000001")

MOCK_IDENTITIES = {
    "admin": ("mock-admin", "管理员"),
    "peter": ("mock-peter", "Peter"),
    "tom": ("mock-tom", "Tom"),
}


def request_mock_token(
    settings: Settings,
    *,
    username: str = "admin",
    scenario: str = "normal",
) -> str:
    response = httpx.post(
        f"{settings.jwt_issuer}/token",
        data={
            "grant_type": "password",
            "username": username,
            "password": "123456",
            "scenario": scenario,
        },
        timeout=5,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


async def provision_test_identity(
    app: FastAPI,
    settings: Settings,
    *,
    username: str,
    permissions: set[str] | None = None,
    scope_type: ScopeType = ScopeType.OWN_ORG,
    active: bool = True,
) -> UserAccount:
    subject, display_name = MOCK_IDENTITIES[username]
    async with app.state.database.session_factory() as session:
        organization = await session.get(OrganizationUnit, TEST_ORG_ID)
        if organization is None:
            organization = OrganizationUnit(
                id=TEST_ORG_ID,
                parent_id=None,
                code="TEST-ROOT",
                name="测试总部",
                unit_type="company",
                path="/TEST-ROOT",
                active=True,
            )
            session.add(organization)
            await session.flush()

        user = UserAccount(
            external_issuer=settings.jwt_issuer,
            external_subject=subject,
            username=username,
            display_name=display_name,
            organization_unit_id=organization.id,
            active=active,
            authz_version=1,
            attributes={"provisioning": "test"},
        )
        session.add(user)
        await session.flush()

        requested_permissions = permissions or set()
        if requested_permissions:
            catalog = await sync_permission_catalog(session)
            role = RoleDefinition(
                code=f"test:{username}:{uuid4().hex}",
                name=f"{username} test role",
                permissions=[],
                system_role=False,
                active=True,
            )
            session.add(role)
            await session.flush()
            for permission in sorted(requested_permissions):
                session.add(
                    RolePermission(
                        role_id=role.id,
                        permission_id=catalog[permission].id,
                    )
                )
            document = {"type": scope_type.value, "organization_unit_ids": []}
            session.add(
                RoleAssignment(
                    user_id=user.id,
                    role_id=role.id,
                    scope_type=scope_type.value,
                    data_scope=document,
                    scope_digest=scope_digest(document),
                    active=True,
                    assigned_by=user.id,
                    grant_reason="test identity provision",
                )
            )
        await session.commit()
        return user


@contextmanager
def authenticated_client(
    settings: Settings,
    *,
    username: str = "admin",
    permissions: set[str] | None = None,
    scope_type: ScopeType = ScopeType.OWN_ORG,
    active: bool = True,
    scenario: str = "normal",
) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(
            partial(
                provision_test_identity,
                app,
                settings,
                username=username,
                permissions=permissions,
                scope_type=scope_type,
                active=active,
            )
        )
        token = request_mock_token(settings, username=username, scenario=scenario)
        client.headers["Authorization"] = f"Bearer {token}"
        yield client

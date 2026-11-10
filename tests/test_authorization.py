from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.handover.domain.models import AudioRecord, AudioTranscriptVersion
from app.modules.operation_event.domain.models import ProductionEvent, ProductionEventVersion
from app.shared.platform.service import scope_digest
from app.shared.security.authorization.models import (
    RoleAssignment,
    RoleDefinition,
    RolePermission,
)
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.repository import (
    bump_authz_version,
    sync_permission_catalog,
)
from app.shared.security.authorization.scopes import ScopeType
from app.shared.security.identity.models import OrganizationUnit, UserAccount
from tests.security_helpers import authenticated_client


@contextmanager
def _client(
    settings,
    *,
    permissions: set[str] | None = None,  # type: ignore[no-untyped-def]
) -> Iterator[TestClient]:
    with authenticated_client(
        settings,
        username="peter",
        permissions=permissions,
        scope_type=ScopeType.OWN_ORG,
    ) as client:
        yield client


async def _grant(
    client: TestClient,
    *,
    user_id: UUID,
    permission: str,
    scope_type: ScopeType,
    organization_ids: set[UUID] | None = None,
) -> UUID:
    async with client.app.state.database.session_factory() as session:
        catalog = await sync_permission_catalog(session)
        role = RoleDefinition(
            code=f"test:{uuid4()}",
            name="测试角色",
            permissions=[],
            system_role=False,
            active=True,
        )
        session.add(role)
        await session.flush()
        session.add(RolePermission(role_id=role.id, permission_id=catalog[permission].id))
        document = {
            "type": scope_type.value,
            "organization_unit_ids": sorted(str(item) for item in organization_ids or set()),
        }
        assignment = RoleAssignment(
            user_id=user_id,
            role_id=role.id,
            scope_type=scope_type.value,
            data_scope=document,
            scope_digest=scope_digest(document),
            active=True,
            assigned_by=user_id,
            grant_reason="authorization test",
        )
        session.add(assignment)
        await bump_authz_version(session, user_id)
        await session.commit()
        return assignment.id


async def _seed_other_organization_and_event(
    client: TestClient,
    actor_id: UUID,
) -> tuple[UUID, UUID]:
    other_org_id = UUID("20000000-0000-0000-0000-000000000002")
    async with client.app.state.database.session_factory() as session:
        session.add(
            OrganizationUnit(
                id=other_org_id,
                parent_id=None,
                code="OTHER-ORG",
                name="其他组织",
                unit_type="company",
                path="/OTHER-ORG",
                active=True,
            )
        )
        event = ProductionEvent(
            organization_unit_id=other_org_id,
            title="其他组织事件",
            event_type="alarm",
            severity="medium",
            occurred_at=datetime.now(UTC),
            business_status="candidate",
            created_by=actor_id,
        )
        session.add(event)
        await session.flush()
        version = ProductionEventVersion(
            event_id=event.id,
            version=1,
            source="manual",
            description="其他组织敏感生产信息",
            structured_data={},
            confidence=None,
            created_by=actor_id,
        )
        session.add(version)
        await session.flush()
        event.current_version_id = version.id
        await session.commit()
        return other_org_id, event.id


def test_database_grant_is_required_and_revocation_is_immediate(settings) -> None:  # type: ignore[no-untyped-def]
    with _client(settings) as client:
        me = client.get("/api/v1/auth/me")
        user_id = UUID(me.json()["user_id"])
        assert client.get("/api/v1/events").status_code == 403
        assignment_id = asyncio.run(
            _grant(
                client,
                user_id=user_id,
                permission=Permissions.EVENT_READ,
                scope_type=ScopeType.OWN_ORG,
            )
        )
        assert client.get("/api/v1/events").status_code == 200

        async def revoke() -> None:
            async with client.app.state.database.session_factory() as session:
                assignment = await session.get(RoleAssignment, assignment_id)
                assert assignment is not None
                assignment.active = False
                assignment.revoked_at = datetime.now(UTC)
                await bump_authz_version(session, user_id)
                await session.commit()

        asyncio.run(revoke())
        assert client.get("/api/v1/events").status_code == 403


def test_permission_scopes_do_not_amplify_across_roles(settings) -> None:  # type: ignore[no-untyped-def]
    with _client(settings) as client:
        me = client.get("/api/v1/auth/me").json()
        user_id = UUID(me["user_id"])
        own_org = UUID(me["organization_unit_id"])
        other_org, event_id = asyncio.run(_seed_other_organization_and_event(client, user_id))
        asyncio.run(
            _grant(
                client,
                user_id=user_id,
                permission=Permissions.EVENT_READ,
                scope_type=ScopeType.GLOBAL,
            )
        )
        asyncio.run(
            _grant(
                client,
                user_id=user_id,
                permission=Permissions.EVENT_REVIEW,
                scope_type=ScopeType.CUSTOM_ORGS,
                organization_ids={own_org},
            )
        )
        assert other_org != own_org
        assert client.get(f"/api/v1/events/{event_id}").status_code == 200
        denied = client.post(f"/api/v1/events/{event_id}:confirm")
        assert denied.status_code == 403
        assert denied.json()["code"] == "PERMISSION_DENIED"


def test_global_configuration_requires_a_global_grant(settings) -> None:  # type: ignore[no-untyped-def]
    with _client(settings, permissions={Permissions.ALL}) as client:
        user_id = UUID(client.get("/api/v1/auth/me").json()["user_id"])
        assert client.get("/api/v1/admin/model-aliases").status_code == 403
        asyncio.run(
            _grant(
                client,
                user_id=user_id,
                permission=Permissions.ALL,
                scope_type=ScopeType.GLOBAL,
            )
        )
        assert client.get("/api/v1/admin/model-aliases").status_code == 200


def test_owned_scope_filters_lists_and_detail_access(settings) -> None:  # type: ignore[no-untyped-def]
    with _client(settings) as client:
        user_id = UUID(client.get("/api/v1/auth/me").json()["user_id"])
        other_org, owned_event_id = asyncio.run(_seed_other_organization_and_event(client, user_id))

        async def seed_unowned_event() -> UUID:
            async with client.app.state.database.session_factory() as session:
                event = ProductionEvent(
                    organization_unit_id=other_org,
                    title="其他用户事件",
                    event_type="alarm",
                    severity="medium",
                    occurred_at=datetime.now(UTC),
                    business_status="candidate",
                    created_by=uuid4(),
                )
                session.add(event)
                await session.commit()
                return event.id

        unowned_event_id = asyncio.run(seed_unowned_event())
        asyncio.run(
            _grant(
                client,
                user_id=user_id,
                permission=Permissions.EVENT_READ,
                scope_type=ScopeType.OWNED,
            )
        )
        event_ids = {item["id"] for item in client.get("/api/v1/events").json()}
        assert str(owned_event_id) in event_ids
        assert str(unowned_event_id) not in event_ids
        assert client.get(f"/api/v1/events/{owned_event_id}").status_code == 200
        assert client.get(f"/api/v1/events/{unowned_event_id}").status_code == 403


def test_access_admin_lifecycle_is_database_backed_and_audited(settings) -> None:  # type: ignore[no-untyped-def]
    with _client(settings, permissions={Permissions.ALL}) as client:
        me = client.get("/api/v1/auth/me").json()
        actor_id = UUID(me["user_id"])
        actor_org_id = UUID(me["organization_unit_id"])
        asyncio.run(
            _grant(
                client,
                user_id=actor_id,
                permission=Permissions.ALL,
                scope_type=ScopeType.GLOBAL,
            )
        )

        role_response = client.post(
            "/api/v1/admin/access/roles",
            json={
                "code": "event_reader_test",
                "name": "事件只读测试角色",
                "permission_codes": [Permissions.EVENT_READ],
            },
        )
        assert role_response.status_code == 201
        role_id = role_response.json()["id"]

        organization_response = client.post(
            "/api/v1/admin/access/organizations",
            json={
                "parent_id": str(actor_org_id),
                "code": "AUTHZ-CHILD",
                "name": "权限测试子组织",
                "unit_type": "department",
            },
        )
        assert organization_response.status_code == 201
        organization_id = organization_response.json()["id"]

        user_response = client.post(
            "/api/v1/admin/access/users",
            json={
                "external_issuer": "test-idp",
                "external_subject": "database-user",
                "username": "database.user",
                "display_name": "数据库授权用户",
                "organization_unit_id": organization_id,
                "attributes": {"employee_no": "T-001"},
            },
        )
        assert user_response.status_code == 201
        target_user_id = user_response.json()["id"]

        assignment_response = client.post(
            "/api/v1/admin/access/role-assignments",
            json={
                "user_id": target_user_id,
                "role_id": role_id,
                "data_scope": {
                    "type": ScopeType.CUSTOM_ORGS.value,
                    "organization_unit_ids": [organization_id],
                },
                "reason": "端到端权限管理测试",
            },
        )
        assert assignment_response.status_code == 201
        assignment_id = assignment_response.json()["id"]

        effective = client.get(f"/api/v1/admin/access/users/{target_user_id}/effective-access")
        assert effective.status_code == 200
        assert effective.json()["permissions"] == [Permissions.EVENT_READ]

        revoke = client.request(
            "DELETE",
            f"/api/v1/admin/access/role-assignments/{assignment_id}",
            json={"reason": "测试撤销并保留审计"},
        )
        assert revoke.status_code == 204
        assert (
            client.get(f"/api/v1/admin/access/users/{target_user_id}/effective-access").json()[
                "permissions"
            ]
            == []
        )

        actions = {item["action"] for item in client.get("/api/v1/admin/access/audit-logs").json()}
        assert {"role_assignment.grant", "role_assignment.revoke"} <= actions


def test_cross_organization_reextract_and_audio_source_are_denied(settings) -> None:  # type: ignore[no-untyped-def]
    with _client(settings) as client:
        me = client.get("/api/v1/auth/me").json()
        user_id = UUID(me["user_id"])
        other_org, event_id = asyncio.run(_seed_other_organization_and_event(client, user_id))
        asyncio.run(
            _grant(
                client,
                user_id=user_id,
                permission=Permissions.EVENT_TRANSFORM,
                scope_type=ScopeType.OWN_ORG,
            )
        )
        asyncio.run(
            _grant(
                client,
                user_id=user_id,
                permission=Permissions.EVENT_EXTRACT,
                scope_type=ScopeType.OWN_ORG,
            )
        )
        assert client.post(f"/api/v1/events/{event_id}:reextract").status_code == 403

        async def seed_audio() -> tuple[UUID, UUID]:
            async with client.app.state.database.session_factory() as session:
                audio = AudioRecord(
                    organization_unit_id=other_org,
                    shift_date=date.today(),
                    shift_code="DAY",
                    filename="secret.wav",
                    object_key=f"secret/{uuid4()}.wav",
                    mime_type="audio/wav",
                    size_bytes=10,
                    upload_status="verified",
                    business_status="transcribed",
                    deleted=False,
                    created_by=user_id,
                )
                session.add(audio)
                await session.flush()
                transcript = AudioTranscriptVersion(
                    audio_record_id=audio.id,
                    version=1,
                    source="manual",
                    full_text="其他组织交接班敏感内容",
                    language="zh",
                    created_by=user_id,
                )
                session.add(transcript)
                await session.flush()
                audio.current_transcript_version_id = transcript.id
                await session.commit()
                return audio.id, transcript.id

        audio_id, transcript_id = asyncio.run(seed_audio())
        denied = client.post(
            "/api/v1/event-extractions",
            json={
                "source_type": "audio_transcript",
                "source_id": str(audio_id),
                "source_version_id": str(transcript_id),
            },
        )
        assert denied.status_code == 403


def test_disabled_database_user_is_rejected(settings) -> None:  # type: ignore[no-untyped-def]
    with _client(settings) as client:
        user_id = UUID(client.get("/api/v1/auth/me").json()["user_id"])

        async def disable() -> None:
            async with client.app.state.database.session_factory() as session:
                user = await session.get(UserAccount, user_id)
                assert user is not None
                user.active = False
                await session.commit()

        asyncio.run(disable())
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 403
        assert response.json()["code"] == "ACCOUNT_DISABLED"


def _has_permission_dependency(dependant) -> bool:  # type: ignore[no-untyped-def]
    if getattr(dependant.call, "required_permission", None):
        return True
    return any(_has_permission_dependency(child) for child in dependant.dependencies)


def test_every_documented_business_route_declares_an_action_permission(settings) -> None:  # type: ignore[no-untyped-def]
    app = create_app(settings)
    public_paths = {
        "/api/v1/auth/me",
    }
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1"):
            continue
        if route.path in public_paths or route.path.startswith("/api/v1/storage/"):
            continue
        if not _has_permission_dependency(route.dependant):
            missing.append(route.path)
    assert missing == []

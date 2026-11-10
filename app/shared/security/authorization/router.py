from __future__ import annotations

# 权限管理API：组织、角色、授权分配以及安全审计查询。
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select

from app.shared.db import SessionDep
from app.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.platform.models import AuditLog
from app.shared.platform.service import add_audit, scope_digest
from app.shared.security.authentication.schemas import AuthenticatedIdentity
from app.shared.security.authorization.dependencies import (
    require_data_scope,
    require_permission,
    scoped_organization_ids,
)
from app.shared.security.authorization.models import (
    PermissionDefinition,
    RoleAssignment,
    RoleDefinition,
    RolePermission,
)
from app.shared.security.authorization.permissions import (
    GLOBAL_ONLY_PERMISSIONS,
    Permissions,
    validate_permission_codes,
)
from app.shared.security.authorization.repository import (
    bump_authz_version,
    descendant_organization_ids,
    load_current_user,
    sync_permission_catalog,
)
from app.shared.security.authorization.schemas import (
    CurrentUser,
    RevokeAssignmentInput,
    RoleAssignmentCreate,
    RoleCreate,
    RoleUpdate,
)
from app.shared.security.authorization.scopes import DataScopeInput, ScopeType
from app.shared.security.identity.models import OrganizationUnit, UserAccount
from app.shared.security.identity.schemas import (
    OrganizationCreate,
    OrganizationUpdate,
    UserCreate,
    UserUpdate,
)

router = APIRouter(prefix="/admin/access", tags=["access-control"])


def _global_grant(user: CurrentUser, permission: str) -> bool:
    """判断用户是否持有指定权限的全局授权。"""
    return any(
        grant.permission in {permission, Permissions.ALL} and grant.scope_type == ScopeType.GLOBAL
        for grant in user.grants
    )


async def _role_permission_codes(session: SessionDep, role_id: UUID) -> set[str]:
    """查询角色当前启用的全部权限代码。"""
    return set(
        await session.scalars(
            select(PermissionDefinition.code)
            .join(RolePermission, RolePermission.permission_id == PermissionDefinition.id)
            .where(RolePermission.role_id == role_id, PermissionDefinition.active.is_(True))
        )
    )


async def _replace_role_permissions(
    session: SessionDep,
    role: RoleDefinition,
    permission_codes: set[str],
) -> None:
    """使用给定权限集合替换角色的现有权限。"""
    validate_permission_codes(permission_codes)  # 拒绝目录外权限
    catalog = await sync_permission_catalog(session)  # 确保权限记录完整
    existing = list(
        await session.scalars(select(RolePermission).where(RolePermission.role_id == role.id))
    )
    for row in existing:
        await session.delete(row)
    await session.flush()  # 先删除旧关系以避免冲突
    for code in sorted(permission_codes):
        session.add(RolePermission(role_id=role.id, permission_id=catalog[code].id))


async def _assert_assignment_scope(
    session: SessionDep,
    actor: CurrentUser,
    target: UserAccount,
    payload: RoleAssignmentCreate,
    role_permissions: set[str],
    *,
    allow_self: bool = False,
) -> None:
    """校验操作者能否按目标范围授予这组角色权限。"""
    if actor.has_global_permission(Permissions.ALL):
        return
    if (
        payload.user_id == actor.user_id
        and not allow_self
        and not actor.has_permission(Permissions.ALL)
    ):
        raise PermissionDeniedError("self_role_assignment")
    scope = payload.data_scope  # 提取待授予的数据范围
    if scope.type != ScopeType.GLOBAL and role_permissions & GLOBAL_ONLY_PERMISSIONS:
        raise PermissionDeniedError("global_permission_requires_global_scope")
    organizations = list(await session.scalars(select(OrganizationUnit)))  # 加载组织树
    active_organization_ids = {item.id for item in organizations if item.active}  # 排除停用组织
    roots = scope.organization_unit_ids or {target.organization_unit_id}  # 确定范围根节点
    if scope.type == ScopeType.OWN_ORG:
        organization_ids = {target.organization_unit_id}
    elif scope.type == ScopeType.CUSTOM_ORGS:
        organization_ids = set(scope.organization_unit_ids)
    elif scope.type == ScopeType.ORG_AND_DESCENDANTS:
        organization_ids = descendant_organization_ids(roots, organizations)
    else:
        organization_ids = set()

    missing_organizations = roots - active_organization_ids  # 找出无效范围节点
    if (
        scope.type
        in {
            ScopeType.OWN_ORG,
            ScopeType.CUSTOM_ORGS,
            ScopeType.ORG_AND_DESCENDANTS,
        }
        and missing_organizations
    ):
        raise NotFoundError("organization_unit", next(iter(missing_organizations)))

    if scope.type == ScopeType.GLOBAL:
        if any(not _global_grant(actor, code) for code in role_permissions):
            raise PermissionDeniedError("delegation_scope")
    elif scope.type in {ScopeType.OWNED, ScopeType.ASSIGNED}:
        if any(not actor.has_permission(code) for code in role_permissions):
            raise PermissionDeniedError("delegation_scope")
    else:
        for code in role_permissions:
            if any(not actor.can_access(code, org_id) for org_id in organization_ids):
                raise PermissionDeniedError("delegation_scope")


@router.get("/permissions")
async def list_permissions(
    session: SessionDep,
    _: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_ROLE))],
) -> list[dict]:
    """列出权限目录中的全部权限。"""
    await sync_permission_catalog(session)  # 返回前同步代码目录
    rows = await session.scalars(
        select(PermissionDefinition).order_by(
            PermissionDefinition.module,
            PermissionDefinition.code,
        )
    )
    return [
        {
            "id": str(row.id),
            "code": row.code,
            "module": row.module,
            "name": row.name,
            "description": row.description,
            "risk_level": row.risk_level,
            "active": row.active,
        }
        for row in rows
    ]


@router.get("/roles")
async def list_roles(
    session: SessionDep,
    _: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_ROLE))],
) -> list[dict]:
    """列出角色及其当前关联的权限。"""
    roles = list(await session.scalars(select(RoleDefinition).order_by(RoleDefinition.code)))
    result = []  # 逐个补充角色权限
    for role in roles:
        result.append(
            {
                "id": str(role.id),
                "code": role.code,
                "name": role.name,
                "description": role.description,
                "system_role": role.system_role,
                "active": role.active,
                "permissions": sorted(await _role_permission_codes(session, role.id)),
            }
        )
    return result


@router.post("/roles", status_code=201)
async def create_role(
    payload: RoleCreate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_ROLE))],
) -> dict:
    """创建自定义角色并绑定初始权限。"""
    if await session.scalar(select(RoleDefinition.id).where(RoleDefinition.code == payload.code)):
        raise ConflictError("role code already exists", code=payload.code)
    role = RoleDefinition(
        code=payload.code,
        name=payload.name,
        description=payload.description,
        permissions=[],
        system_role=False,
        active=True,
    )
    session.add(role)
    await session.flush()
    await _replace_role_permissions(session, role, payload.permission_codes)  # 建立权限关系
    await add_audit(
        session,
        user=user,
        action="role.create",
        resource_type="role",
        resource_id=role.id,
        after={"code": role.code, "permissions": sorted(payload.permission_codes)},
    )
    await session.commit()
    return {"id": str(role.id), "code": role.code}


@router.patch("/roles/{role_id}")
async def update_role(
    role_id: UUID,
    payload: RoleUpdate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_ROLE))],
) -> dict:
    """更新角色信息，并重新验证受影响的授权。"""
    role = await session.get(RoleDefinition, role_id)
    if role is None:
        raise NotFoundError("role", role_id)
    if role.system_role and not user.has_permission(Permissions.ALL):
        raise PermissionDeniedError("system_role")
    before_permissions = await _role_permission_codes(session, role.id)  # 保存变更前权限
    before = {
        "name": role.name,
        "active": role.active,
        "permissions": sorted(before_permissions),
    }  # 生成审计快照
    prospective_permissions = (
        payload.permission_codes if payload.permission_codes is not None else before_permissions
    )
    grants_access = payload.permission_codes is not None or (  # 判断是否可能扩大访问权
        payload.active is True and not role.active
    )
    if grants_access:
        assignments = list(  # 找出所有受影响的生效授权
            await session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.role_id == role.id,
                    RoleAssignment.active.is_(True),
                )
            )
        )
        for assignment in assignments:
            target = await session.get(UserAccount, assignment.user_id)
            if target is None:
                raise NotFoundError("user_account", assignment.user_id)
            require_data_scope(
                user,
                target.organization_unit_id,
                Permissions.ADMIN_PERMISSION,
            )
            assignment_payload = RoleAssignmentCreate(
                user_id=assignment.user_id,
                role_id=assignment.role_id,
                data_scope=DataScopeInput.model_validate(assignment.data_scope),
                effective_from=assignment.effective_from,
                expires_at=assignment.expires_at,
                reason="role permission update validation",
            )
            await _assert_assignment_scope(
                session,
                user,
                target,
                assignment_payload,
                prospective_permissions,
            )
    if payload.name is not None:
        role.name = payload.name
    if payload.description is not None:
        role.description = payload.description
    if payload.active is not None:
        role.active = payload.active
    if payload.permission_codes is not None:
        await _replace_role_permissions(session, role, payload.permission_codes)
    target_users = set(  # 收集需要刷新权限版本的用户
        await session.scalars(
            select(RoleAssignment.user_id).where(
                RoleAssignment.role_id == role.id,
                RoleAssignment.active.is_(True),
            )
        )
    )
    for target_user_id in target_users:
        await bump_authz_version(session, target_user_id)
    after_permissions = (
        payload.permission_codes if payload.permission_codes is not None else before_permissions
    )
    await add_audit(
        session,
        user=user,
        action="role.update",
        resource_type="role",
        resource_id=role.id,
        before=before,
        after={"name": role.name, "active": role.active, "permissions": sorted(after_permissions)},
    )
    await session.commit()
    return {"id": str(role.id), "active": role.active}


@router.get("/organizations")
async def list_organizations(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_ORG))],
) -> list[dict]:
    """按当前用户的数据范围列出组织单元。"""
    statement = select(OrganizationUnit).order_by(OrganizationUnit.path)
    scope = scoped_organization_ids(user, Permissions.ADMIN_ORG)  # 获取可管理范围
    if scope is not None:
        statement = statement.where(OrganizationUnit.id.in_(scope))
    rows = await session.scalars(statement)
    return [
        {
            "id": str(row.id),
            "parent_id": str(row.parent_id) if row.parent_id else None,
            "code": row.code,
            "name": row.name,
            "unit_type": row.unit_type,
            "path": row.path,
            "active": row.active,
        }
        for row in rows
    ]


@router.post("/organizations", status_code=201)
async def create_organization(
    payload: OrganizationCreate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_ORG))],
) -> dict:
    """在有权管理的父节点下创建组织单元。"""
    parent = (
        await session.get(OrganizationUnit, payload.parent_id) if payload.parent_id else None
    )  # 查询父组织
    if payload.parent_id and parent is None:
        raise NotFoundError("organization_unit", payload.parent_id)
    if parent is not None and not parent.active:
        raise NotFoundError("organization_unit", payload.parent_id)
    if parent is not None:
        require_data_scope(user, parent.id, Permissions.ADMIN_ORG)
    elif not _global_grant(user, Permissions.ADMIN_ORG):
        raise PermissionDeniedError("root_organization")
    row = OrganizationUnit(
        parent_id=parent.id if parent else None,
        code=payload.code,
        name=payload.name,
        unit_type=payload.unit_type,
        path=f"{parent.path if parent else ''}/{payload.code}",
        active=True,
    )
    session.add(row)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="organization.create",
        resource_type="organization_unit",
        resource_id=row.id,
        after=payload.model_dump(mode="json"),
    )
    await session.commit()
    return {"id": str(row.id), "path": row.path}


@router.patch("/organizations/{organization_id}")
async def update_organization(
    organization_id: UUID,
    payload: OrganizationUpdate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_ORG))],
) -> dict:
    """更新指定组织单元的可变属性。"""
    row = await session.get(OrganizationUnit, organization_id)
    if row is None:
        raise NotFoundError("organization_unit", organization_id)
    require_data_scope(user, row.id, Permissions.ADMIN_ORG)
    before = {"name": row.name, "unit_type": row.unit_type, "active": row.active}  # 保存审计快照
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await add_audit(
        session,
        user=user,
        action="organization.update",
        resource_type="organization_unit",
        resource_id=row.id,
        before=before,
        after={"name": row.name, "unit_type": row.unit_type, "active": row.active},
    )
    await session.commit()
    return {"id": str(row.id), "active": row.active}


@router.get("/users")
async def list_users(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_USER))],
) -> list[dict]:
    """按当前用户的数据范围列出用户账号。"""
    statement = select(UserAccount).order_by(UserAccount.username)
    scope = scoped_organization_ids(user, Permissions.ADMIN_USER)  # 获取可管理范围
    if scope is not None:
        statement = statement.where(UserAccount.organization_unit_id.in_(scope))
    rows = await session.scalars(statement)
    return [
        {
            "id": str(row.id),
            "external_issuer": row.external_issuer,
            "external_subject": row.external_subject,
            "username": row.username,
            "display_name": row.display_name,
            "organization_unit_id": str(row.organization_unit_id),
            "active": row.active,
            "authz_version": row.authz_version,
        }
        for row in rows
    ]


@router.post("/users", status_code=201)
async def create_user(
    payload: UserCreate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_USER))],
) -> dict:
    """在指定组织中创建并启用内部用户账号。"""
    organization = await session.get(OrganizationUnit, payload.organization_unit_id)  # 校验所属组织
    if organization is None or not organization.active:
        raise NotFoundError("organization_unit", payload.organization_unit_id)
    require_data_scope(user, organization.id, Permissions.ADMIN_USER)
    duplicate = await session.scalar(  # 检查用户名及外部身份冲突
        select(UserAccount.id).where(
            (UserAccount.username == payload.username)
            | (
                (UserAccount.external_issuer == payload.external_issuer)
                & (UserAccount.external_subject == payload.external_subject)
            )
        )
    )
    if duplicate:
        raise ConflictError("user identity or username already exists")
    row = UserAccount(**payload.model_dump(), active=True, authz_version=1)
    session.add(row)
    await session.flush()
    await add_audit(
        session,
        user=user,
        action="user.create",
        resource_type="user_account",
        resource_id=row.id,
        after={"username": row.username, "organization_unit_id": str(row.organization_unit_id)},
    )
    await session.commit()
    return {"id": str(row.id), "username": row.username}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    session: SessionDep,
    actor: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_USER))],
) -> dict:
    """更新用户资料、所属组织或启用状态。"""
    row = await session.get(UserAccount, user_id)
    if row is None:
        raise NotFoundError("user_account", user_id)
    require_data_scope(actor, row.organization_unit_id, Permissions.ADMIN_USER)
    if payload.active is False and row.id == actor.user_id:
        raise ConflictError("an administrator cannot disable the current account")
    if payload.organization_unit_id is not None:
        organization = await session.get(OrganizationUnit, payload.organization_unit_id)
        if organization is None or not organization.active:
            raise NotFoundError("organization_unit", payload.organization_unit_id)
        require_data_scope(actor, organization.id, Permissions.ADMIN_USER)
    before = {  # 保存审计快照
        "display_name": row.display_name,
        "organization_unit_id": str(row.organization_unit_id),
        "active": row.active,
    }
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    row.authz_version += 1  # 使旧权限缓存失效
    await add_audit(
        session,
        user=actor,
        action="user.update",
        resource_type="user_account",
        resource_id=row.id,
        before=before,
        after={
            "display_name": row.display_name,
            "organization_unit_id": str(row.organization_unit_id),
            "active": row.active,
        },
    )
    await session.commit()
    return {"id": str(row.id), "active": row.active, "authz_version": row.authz_version}


@router.get("/role-assignments")
async def list_role_assignments(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_PERMISSION))],
    target_user_id: UUID | None = Query(default=None),
) -> list[dict]:
    """按权限范围列出角色授权记录。"""
    statement = (  # 联查角色和目标用户
        select(RoleAssignment, RoleDefinition, UserAccount)
        .join(RoleDefinition, RoleDefinition.id == RoleAssignment.role_id)
        .join(UserAccount, UserAccount.id == RoleAssignment.user_id)
        .order_by(RoleAssignment.created_at.desc())
    )
    if target_user_id:
        statement = statement.where(RoleAssignment.user_id == target_user_id)
    scope = scoped_organization_ids(user, Permissions.ADMIN_PERMISSION)  # 限制可管理组织
    if scope is not None:
        statement = statement.where(UserAccount.organization_unit_id.in_(scope))
    rows = (await session.execute(statement)).all()
    return [
        {
            "id": str(assignment.id),
            "user_id": str(assignment.user_id),
            "role_id": str(assignment.role_id),
            "role_code": role.code,
            "scope_type": assignment.scope_type,
            "data_scope": assignment.data_scope,
            "active": assignment.active,
            "effective_from": assignment.effective_from,
            "expires_at": assignment.expires_at,
            "grant_reason": assignment.grant_reason,
            "revoke_reason": assignment.revoke_reason,
        }
        for assignment, role, _ in rows
    ]


@router.post("/role-assignments", status_code=201)
async def create_role_assignment(
    payload: RoleAssignmentCreate,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_PERMISSION))],
) -> dict:
    """校验委派范围后向用户授予角色。"""
    target = await session.get(UserAccount, payload.user_id)  # 查询授权目标
    if target is None or not target.active:
        raise NotFoundError("user_account", payload.user_id)
    role = await session.get(RoleDefinition, payload.role_id)
    if role is None or not role.active:
        raise NotFoundError("role", payload.role_id)
    require_data_scope(user, target.organization_unit_id, Permissions.ADMIN_PERMISSION)
    permission_codes = await _role_permission_codes(session, role.id)  # 加载待授予权限
    await _assert_assignment_scope(session, user, target, payload, permission_codes)  # 校验委派边界
    document = payload.data_scope.as_document()  # 规范化持久化范围
    duplicate = await session.scalar(  # 防止重复生效授权
        select(RoleAssignment.id).where(
            RoleAssignment.user_id == payload.user_id,
            RoleAssignment.role_id == payload.role_id,
            RoleAssignment.scope_digest == scope_digest(document),
            RoleAssignment.active.is_(True),
        )
    )
    if duplicate:
        raise ConflictError("an equivalent active role assignment already exists")
    assignment = RoleAssignment(
        user_id=payload.user_id,
        role_id=payload.role_id,
        scope_type=payload.data_scope.type.value,
        data_scope=document,
        scope_digest=scope_digest(document),
        active=True,
        effective_from=payload.effective_from,
        expires_at=payload.expires_at,
        assigned_by=user.user_id,
        grant_reason=payload.reason,
    )
    session.add(assignment)
    await session.flush()
    await bump_authz_version(session, payload.user_id)
    await add_audit(
        session,
        user=user,
        action="role_assignment.grant",
        resource_type="role_assignment",
        resource_id=assignment.id,
        after={
            "user_id": str(payload.user_id),
            "role_id": str(payload.role_id),
            "data_scope": document,
        },
        reason=payload.reason,
    )
    await session.commit()
    return {"id": str(assignment.id)}


@router.delete("/role-assignments/{assignment_id}", status_code=204)
async def revoke_role_assignment(
    assignment_id: UUID,
    payload: RevokeAssignmentInput,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_PERMISSION))],
) -> Response:
    """撤销角色授权并刷新目标用户的权限版本。"""
    assignment = await session.get(RoleAssignment, assignment_id)
    if assignment is None:
        raise NotFoundError("role_assignment", assignment_id)
    target = await session.get(UserAccount, assignment.user_id)
    if target is None:
        raise NotFoundError("user_account", assignment.user_id)
    require_data_scope(user, target.organization_unit_id, Permissions.ADMIN_PERMISSION)
    if not assignment.active:
        raise ConflictError("role assignment is already inactive")
    role_permissions = await _role_permission_codes(session, assignment.role_id)  # 加载原角色权限
    assignment_payload = RoleAssignmentCreate(  # 重建参数以复用范围校验
        user_id=assignment.user_id,
        role_id=assignment.role_id,
        data_scope=DataScopeInput.model_validate(assignment.data_scope),
        effective_from=assignment.effective_from,
        expires_at=assignment.expires_at,
        reason=payload.reason,
    )
    await _assert_assignment_scope(
        session,
        user,
        target,
        assignment_payload,
        role_permissions,
        allow_self=True,
    )
    assignment.active = False
    assignment.revoked_at = datetime.now(UTC)  # 记录统一时区的撤销时间
    assignment.revoked_by = user.user_id
    assignment.revoke_reason = payload.reason
    await bump_authz_version(session, assignment.user_id)
    await add_audit(
        session,
        user=user,
        action="role_assignment.revoke",
        resource_type="role_assignment",
        resource_id=assignment.id,
        before={"active": True},
        after={"active": False},
        reason=payload.reason,
    )
    await session.commit()
    return Response(status_code=204)


@router.get("/users/{user_id}/effective-access")
async def read_effective_access(
    user_id: UUID,
    session: SessionDep,
    actor: Annotated[
        CurrentUser,
        Depends(require_permission(Permissions.ADMIN_PERMISSION)),
    ],
) -> dict:
    """读取指定用户聚合后的实际角色与权限。"""
    target = await session.get(UserAccount, user_id)
    if target is None:
        raise NotFoundError("user_account", user_id)
    require_data_scope(actor, target.organization_unit_id, Permissions.ADMIN_PERMISSION)
    current = await load_current_user(  # 使用目标身份计算实时权限
        session,
        AuthenticatedIdentity(
            user_id=target.id,
            issuer=target.external_issuer,
            subject=target.external_subject,
            username=target.username,
            display_name=target.display_name,
            organization_unit_id=target.organization_unit_id,
            authz_version=target.authz_version,
        ),
    )
    return {
        "user_id": str(target.id),
        "roles": sorted(current.roles),
        "permissions": sorted(current.permissions),
        "grants": [grant.model_dump(mode="json") for grant in current.grants],
    }


@router.get("/audit-logs")
async def list_audit_logs(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_permission(Permissions.ADMIN_AUDIT))],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """按当前用户的数据范围列出最近的安全审计日志。"""
    statement = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)
    scope = scoped_organization_ids(user, Permissions.ADMIN_AUDIT)  # 限制可查看组织
    if scope is not None:
        statement = statement.where(AuditLog.organization_unit_id.in_(scope))
    rows = await session.scalars(statement)
    return [
        {
            "id": str(row.id),
            "occurred_at": row.occurred_at,
            "actor_id": str(row.actor_id),
            "organization_unit_id": str(row.organization_unit_id),
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": str(row.resource_id),
            "before": row.before_data,
            "after": row.after_data,
            "request_id": row.request_id,
            "reason": row.reason,
        }
        for row in rows
    ]

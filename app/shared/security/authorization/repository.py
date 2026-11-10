from __future__ import annotations

# RBAC数据访问层：同步权限目录并计算用户当前有效授权。
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.security.authentication.schemas import AuthenticatedIdentity
from app.shared.security.authorization.models import (
    PermissionDefinition,
    RoleAssignment,
    RoleDefinition,
    RolePermission,
)
from app.shared.security.authorization.permissions import (
    BUILTIN_ROLES,
    PERMISSION_CATALOG,
)
from app.shared.security.authorization.schemas import CurrentUser, EffectiveGrant
from app.shared.security.authorization.scopes import ScopeType
from app.shared.security.identity.models import OrganizationUnit, UserAccount


async def sync_permission_catalog(session: AsyncSession) -> dict[str, PermissionDefinition]:
    """将代码中的权限目录同步到数据库。"""
    existing = {
        item.code: item for item in await session.scalars(select(PermissionDefinition))
    }  # 从权限表查询里面的所有权限，看看数据库里有哪些权限
    for code, spec in PERMISSION_CATALOG.items():
        row = existing.get(code)  # 遍历程序中的code
        if row is None:  # 程序中的权限在数据库中没有
            row = PermissionDefinition(  # 创建权限表的对象
                code=code,
                module=spec.module,
                name=spec.name,
                risk_level=spec.risk_level,
                active=True,
            )
            session.add(row)  # 执行insert
            existing[code] = row
        else:
            row.module = spec.module
            row.name = spec.name
            row.risk_level = spec.risk_level
    await session.flush()  # 让新增权限获得主键
    return existing


async def sync_builtin_roles(session: AsyncSession) -> dict[str, RoleDefinition]:
    """同步内置角色及其权限关系。"""
    permissions = await sync_permission_catalog(session)  # 确保权限外键已存在
    roles = {
        item.code: item for item in await session.scalars(select(RoleDefinition))
    }  # 建立角色索引
    for code, (name, permission_codes) in BUILTIN_ROLES.items():
        role = roles.get(code)
        if role is None:
            role = RoleDefinition(
                code=code,
                name=name,
                description="Built-in role maintained by the authorization catalog.",
                permissions=[],
                system_role=True,
                active=True,
            )
            session.add(role)
            await session.flush()
            roles[code] = role
        existing_codes = set(  # 查询角色当前关联的权限
            await session.scalars(
                select(PermissionDefinition.code)
                .join(RolePermission, RolePermission.permission_id == PermissionDefinition.id)
                .where(RolePermission.role_id == role.id)
            )
        )
        if existing_codes == permission_codes:
            continue
        await session.execute(
            delete(RolePermission).where(RolePermission.role_id == role.id)
        )  # 清理旧关联
        for permission_code in sorted(permission_codes):
            session.add(
                RolePermission(
                    role_id=role.id,
                    permission_id=permissions[permission_code].id,
                )
            )
    await session.flush()
    return roles


def descendant_organization_ids(
    roots: set[UUID],  # 传任何一个组织的uuid
    organizations: list[OrganizationUnit],  # 从数据库中查询出来的系统中所有的组织
) -> set[UUID]:
    # 这里organizations是一个用list存放的树结构，整个代码就是对这个结构进行遍历
    """计算一组组织节点包含自身在内的全部有效后代。"""
    children: dict[UUID, set[UUID]] = defaultdict(set)  # 构建父子索引
    active_ids = {item.id for item in organizations if item.active}  # 排除停用组织
    for item in organizations:
        if item.active and item.parent_id is not None:
            children[item.parent_id].add(item.id)
    result = set(roots) & active_ids  # 仅从有效根节点开始
    pending = list(result)  # 使用栈遍历组织树
    while pending:
        current = pending.pop()
        for child in children[current] - result:
            result.add(child)
            pending.append(child)
    return result


def _scope_ids(
    assignment: RoleAssignment,  # 角色分配表
    identity: AuthenticatedIdentity,  # 用户身份相关信息，比如用户id，所属组织等
    organizations: list[OrganizationUnit],  # 系统中所有的组织
) -> tuple[ScopeType, set[UUID]]:  # 某一个ScopeType及其对应的组织
    # 也可以理解为知道用户信息和角色信息，去查询这个用户的scope具体范围，即哪些组织
    """解析角色授权的数据范围类型（ScopeType中的某一项）及有效组织集合(根据前面ScopeType的内容决定有哪些组织可以访问)。"""
    scope_type = ScopeType(assignment.scope_type)  # 将持久化值转换为枚举
    raw_ids = assignment.data_scope.get("organization_unit_ids", [])  # 读取自定义组织范围
    requested = {UUID(str(item)) for item in raw_ids}  # 统一转换为 UUID
    active_ids = {item.id for item in organizations if item.active}  # 过滤停用组织
    if scope_type == ScopeType.OWN_ORG:
        return scope_type, {identity.organization_unit_id}
    if scope_type == ScopeType.CUSTOM_ORGS:
        return scope_type, requested & active_ids
    if scope_type == ScopeType.ORG_AND_DESCENDANTS:
        return scope_type, descendant_organization_ids(
            requested or {identity.organization_unit_id},
            organizations,
        )
    return scope_type, set()


async def load_current_user(
    session: AsyncSession,  # 数据库连接
    identity: AuthenticatedIdentity,  # 基本的用户身份信息
) -> CurrentUser:
    """聚合用户当前生效的角色、权限和数据范围。"""
    now = datetime.now(UTC)  # 固定本次查询的时间基准
    rows = list(  # 查询启用授权(带有效期)
        (
            await session.execute(
                select(RoleAssignment, RoleDefinition)
                .join(RoleDefinition, RoleDefinition.id == RoleAssignment.role_id)
                .where(
                    RoleAssignment.user_id == identity.user_id,
                    RoleAssignment.active.is_(True),
                    RoleDefinition.active.is_(True),
                    or_(
                        RoleAssignment.effective_from.is_(None),
                        RoleAssignment.effective_from <= now,
                    ),
                    or_(RoleAssignment.expires_at.is_(None), RoleAssignment.expires_at > now),
                )
            )
        ).all()
    )
    role_ids = {role.id for _, role in rows}  # 提取需要加载权限的角色
    permission_rows = (  # 批量查询角色权限映射
        list(
            (
                await session.execute(
                    select(RolePermission.role_id, PermissionDefinition.code)
                    .join(
                        PermissionDefinition,
                        PermissionDefinition.id == RolePermission.permission_id,
                    )
                    .where(
                        RolePermission.role_id.in_(role_ids),
                        PermissionDefinition.active.is_(True),
                    )
                )
            ).all()
        )
        if role_ids
        else []
    )
    permissions_by_role: dict[UUID, set[str]] = defaultdict(set)  # 按角色归集权限
    # 准备好字典，键是角色的id，值是这个角色对应的权限code的集合
    for role_id, permission_code in permission_rows:
        permissions_by_role[role_id].add(permission_code)

    organizations = list(await session.scalars(select(OrganizationUnit)))  # 为范围计算加载组织树

    grants: list[EffectiveGrant] = []  # 展开角色形成有效授权
    # 当前项目做到的是某个角色的某个权限下可以访问哪些组织资源
    # 比如xxx.READ能读哪些资源
    # ABC.READ可以访问组织1，3，5
    # ABC.EDIT可以访问组织1
    for assignment, role in rows:  # 这个for遍历角色
        scope_type, organization_ids = _scope_ids(assignment, identity, organizations)
        for permission in permissions_by_role[role.id]:  # 根据角色便利权限
            grants.append(
                EffectiveGrant(
                    role_code=role.code,
                    permission=permission,
                    scope_type=scope_type,
                    organization_unit_ids=organization_ids,
                    assignment_id=assignment.id,
                )
            )
    permissions = {grant.permission for grant in grants}
    organization_scope = {item for grant in grants for item in grant.organization_unit_ids}
    return CurrentUser(
        user_id=identity.user_id,
        subject=identity.subject,
        username=identity.username,
        display_name=identity.display_name,
        organization_unit_id=identity.organization_unit_id,
        roles={role.code for _, role in rows},
        permissions=permissions,
        organization_scope=organization_scope,
        grants=grants,
        authz_version=identity.authz_version,
    )


async def bump_authz_version(session: AsyncSession, user_id: UUID) -> None:
    """递增用户的授权版本，使旧权限缓存失效。"""
    user = await session.get(UserAccount, user_id)  # 锁定待更新账号
    if user is not None:
        user.authz_version += 1

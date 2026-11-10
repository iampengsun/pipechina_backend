from collections.abc import Awaitable, Callable

# 构造FastAPI权限依赖，并把角色数据范围转换成SQL过滤条件。
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import false, or_, true
from sqlalchemy.sql.elements import ColumnElement

from app.shared.db import SessionDep
from app.shared.errors import PermissionDeniedError
from app.shared.security.authentication.dependencies import AuthenticatedIdentityDep
from app.shared.security.authentication.schemas import AuthenticatedIdentity
from app.shared.security.authorization.permissions import GLOBAL_ONLY_PERMISSIONS
from app.shared.security.authorization.repository import load_current_user
from app.shared.security.authorization.schemas import CurrentUser
from app.shared.security.authorization.scopes import ScopeType


async def get_current_user(
    identity: AuthenticatedIdentityDep,
    session: SessionDep,
) -> CurrentUser:
    """加载认证身份对应的当前用户权限视图。"""
    assert isinstance(identity, AuthenticatedIdentity)
    return await load_current_user(session, identity)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_permission(
    permission: str,
) -> Callable[[CurrentUser], Awaitable[CurrentUser]]:
    """生成用于校验指定权限的 FastAPI 依赖。"""

    async def dependency(user: CurrentUserDep) -> CurrentUser:
        """校验当前用户是否具备依赖要求的权限。"""
        permitted = (
            user.has_global_permission(permission)
            if permission in GLOBAL_ONLY_PERMISSIONS
            else user.has_permission(permission)
        )  # 全局权限只接受全局授权
        if not permitted:
            raise PermissionDeniedError(permission)
        return user

    dependency.required_permission = permission  # type: ignore[attr-defined]
    return dependency


require_access = require_permission


def require_data_scope(
    user: CurrentUser,
    organization_unit_id: UUID,
    permission: str,
    *,
    owner_id: UUID | None = None,
    assignee_id: UUID | None = None,
) -> None:
    """校验用户对单个业务资源的数据范围权限。"""
    if not user.can_access(
        permission,
        organization_unit_id,
        owner_id=owner_id,
        assignee_id=assignee_id,
    ):
        raise PermissionDeniedError(permission)


def scoped_organization_ids(user: CurrentUser, permission: str) -> set[UUID] | None:
    """获取用户在指定权限下可访问的组织集合。"""
    if not user.has_permission(permission):
        raise PermissionDeniedError(permission)
    return user.organization_scope_for(permission)


def data_scope_clause(
    user: CurrentUser,
    permission: str,
    organization_column: Any,
    *,
    owner_column: Any | None = None,
    assignee_column: Any | None = None,
) -> ColumnElement[bool]:
    """根据用户授权构造 SQLAlchemy 数据范围过滤条件。"""
    # 这个代码等于是在帮我们构建一条select语句的条件
    # 这里是在动态生成sql语句
    matching = [  # matching暂时想成是where，这里在构造where条件
        grant for grant in user.grants if grant.permission in {permission, "*"}
    ]  # 筛选相关授权
    if any(grant.scope_type == ScopeType.GLOBAL for grant in matching):
        return true()
    organization_ids = {  # 汇总组织类授权的可见范围
        organization_id
        for grant in matching
        if grant.scope_type
        in {ScopeType.OWN_ORG, ScopeType.ORG_AND_DESCENDANTS, ScopeType.CUSTOM_ORGS}
        for organization_id in grant.organization_unit_ids
    }
    clauses: list[ColumnElement[bool]] = []  # 收集各类范围条件
    if organization_ids:
        clauses.append(organization_column.in_(organization_ids))
        # 在基础SQL语句的基础上加上 or organization in(1,3,5)
    if owner_column is not None and any(grant.scope_type == ScopeType.OWNED for grant in matching):
        clauses.append(owner_column == user.user_id)
        # 在基础SQL语句的基础上加上 or owner_column = user.user_id
    if assignee_column is not None and any(
        grant.scope_type == ScopeType.ASSIGNED for grant in matching
    ):
        clauses.append(assignee_column == user.user_id)
    return or_(*clauses) if clauses else false()  # 以上几个条件用or拼接

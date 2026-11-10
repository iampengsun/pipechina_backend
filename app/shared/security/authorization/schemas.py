from datetime import datetime

# 权限管理接口的请求和响应模型，并在入口处校验数据范围参数。
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.shared.security.authorization.scopes import DataScopeInput, ScopeType


class EffectiveGrant(BaseModel):
    """表示用户经角色计算后得到的一项有效授权。"""

    role_code: str
    permission: str
    scope_type: ScopeType
    organization_unit_ids: set[UUID] = Field(default_factory=set)
    assignment_id: UUID | None = None


class CurrentUser(BaseModel):
    """汇总当前用户身份及其生效的角色和权限。"""

    user_id: UUID
    subject: str
    username: str
    display_name: str
    organization_unit_id: UUID
    roles: set[str] = Field(default_factory=set)
    permissions: set[str] = Field(default_factory=set)
    organization_scope: set[UUID] = Field(default_factory=set)
    grants: list[EffectiveGrant] = Field(default_factory=list, exclude=True)
    authz_version: int = 1

    @model_validator(mode="after")
    def build_internal_worker_grants(self) -> CurrentUser:
        """为仅提供权限集合的内部调用补建授权明细。"""
        if self.grants or not self.permissions:
            return self
        scope_type = (
            ScopeType.CUSTOM_ORGS if self.organization_scope else ScopeType.OWN_ORG
        )  # 推断范围类型
        scope = self.organization_scope or {self.organization_unit_id}  # 缺省限制在本组织
        self.grants = [
            EffectiveGrant(
                role_code=next(iter(self.roles), "internal"),
                permission=permission,
                scope_type=scope_type,
                organization_unit_ids=set(scope),
            )
            for permission in self.permissions
        ]
        return self

    def has_permission(self, permission: str) -> bool:
        """判断用户是否拥有指定权限。"""
        return any(grant.permission in {permission, "*"} for grant in self.grants)

    def has_global_permission(self, permission: str) -> bool:
        """判断用户是否拥有指定权限的全局授权。"""
        return any(
            grant.permission in {permission, "*"} and grant.scope_type == ScopeType.GLOBAL
            for grant in self.grants
        )

    def organization_scope_for(self, permission: str) -> set[UUID] | None:
        """返回权限覆盖的组织集合，全局授权返回空限制。"""
        matching = [
            grant for grant in self.grants if grant.permission in {permission, "*"}
        ]  # 筛选相关授权
        if any(grant.scope_type == ScopeType.GLOBAL for grant in matching):
            return None
        return {
            organization_id
            for grant in matching
            for organization_id in grant.organization_unit_ids
            if grant.scope_type
            in {ScopeType.OWN_ORG, ScopeType.ORG_AND_DESCENDANTS, ScopeType.CUSTOM_ORGS}
        }

    def can_access(
        self,
        permission: str,
        organization_unit_id: UUID,
        *,
        owner_id: UUID | None = None,
        assignee_id: UUID | None = None,
    ) -> bool:
        """判断用户能否访问给定组织、所有者或处理人的资源。"""
        for grant in self.grants:
            if grant.permission not in {permission, "*"}:
                continue
            if grant.scope_type == ScopeType.GLOBAL:
                return True
            if (
                grant.scope_type
                in {
                    ScopeType.OWN_ORG,
                    ScopeType.ORG_AND_DESCENDANTS,
                    ScopeType.CUSTOM_ORGS,
                }
                and organization_unit_id in grant.organization_unit_ids
            ):
                return True
            if grant.scope_type == ScopeType.OWNED and owner_id == self.user_id:
                return True
            if grant.scope_type == ScopeType.ASSIGNED and assignee_id == self.user_id:
                return True
        return False


class RoleCreate(BaseModel):
    """定义创建角色时可提交的数据。"""

    code: str = Field(pattern=r"^[a-z][a-z0-9:_-]{1,79}$")
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    permission_codes: set[str] = Field(default_factory=set)


class RoleUpdate(BaseModel):
    """定义更新角色时可修改的数据。"""

    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    active: bool | None = None
    permission_codes: set[str] | None = None


class RoleAssignmentCreate(BaseModel):
    """定义向用户授予角色时所需的数据。"""

    user_id: UUID
    role_id: UUID
    data_scope: DataScopeInput = Field(default_factory=DataScopeInput)
    effective_from: datetime | None = None
    expires_at: datetime | None = None
    reason: str = Field(min_length=2, max_length=2000)

    @model_validator(mode="after")
    def validate_validity(self) -> RoleAssignmentCreate:
        """确保授权的失效时间晚于生效时间。"""
        if self.effective_from and self.expires_at and self.expires_at <= self.effective_from:
            raise ValueError("expires_at must be later than effective_from")
        return self


class RevokeAssignmentInput(BaseModel):
    """定义撤销角色授权时必须填写的原因。"""

    reason: str = Field(min_length=2, max_length=2000)

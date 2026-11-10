from uuid import UUID

# 组织和用户目录管理接口使用的请求、响应模型。
from pydantic import BaseModel, Field


class OrganizationCreate(BaseModel):
    """定义创建组织单元时可提交的数据。"""

    parent_id: UUID | None = None
    code: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{1,63}$")
    name: str = Field(min_length=2, max_length=200)
    unit_type: str = Field(default="department", min_length=2, max_length=32)


class OrganizationUpdate(BaseModel):
    """定义更新组织单元时可修改的数据。"""

    name: str | None = Field(default=None, min_length=2, max_length=200)
    unit_type: str | None = Field(default=None, min_length=2, max_length=32)
    active: bool | None = None


class UserCreate(BaseModel):
    """定义创建内部用户账号时所需的数据。"""

    external_issuer: str = Field(min_length=3, max_length=300)
    external_subject: str = Field(min_length=1, max_length=200)
    username: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]{1,99}$")
    display_name: str = Field(min_length=1, max_length=200)
    organization_unit_id: UUID
    attributes: dict = Field(default_factory=dict)


class UserUpdate(BaseModel):
    """定义更新内部用户账号时可修改的数据。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    organization_unit_id: UUID | None = None
    active: bool | None = None
    attributes: dict | None = None

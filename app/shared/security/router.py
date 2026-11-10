from fastapi import APIRouter

# 当前用户入口，用于前端获取登录身份和已经计算好的有效权限。
from app.shared.security.authorization.dependencies import CurrentUserDep

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
async def me(user: CurrentUserDep) -> dict:
    """返回当前登录用户及其有效权限信息。"""
    return user.model_dump(mode="json")

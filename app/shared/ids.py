from uuid import UUID, uuid4


# 统一生成UUID主键，便于模型通过default引用。
def new_id() -> UUID:
    """生成一个随机UUID业务主键。"""

    return uuid4()

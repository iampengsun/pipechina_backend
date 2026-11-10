from __future__ import annotations  # 延迟解析类型注解，避免前向引用问题

from collections.abc import AsyncIterator  # 异步迭代器类型
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

# 统一定义数据库约束和索引的命名规则，便于 Alembic 生成稳定的迁移脚本
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",  # 普通索引名称
    "uq": "uq_%(table_name)s_%(column_0_name)s",  # 唯一约束名称
    "ck": "ck_%(table_name)s_%(constraint_name)s",  # 检查约束名称
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",  # 外键约束名称
    "pk": "pk_%(table_name)s",  # 主键约束名称
}


class Base(DeclarativeBase):
    """所有 SQLAlchemy ORM 模型的基类。"""

    # 为所有继承 Base 的数据表应用统一的约束命名规则
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Database:
    """封装异步数据库引擎和数据库会话工厂。"""

    def __init__(self, url: str, *, use_null_pool: bool = False) -> None:
        """创建异步引擎和会话工厂。"""

        self.url = url  # 保存数据库连接地址

        engine_options: dict = {"pool_pre_ping": True}
        if use_null_pool:
            # 测试辅助代码可能跨事件循环执行，因此不复用asyncpg连接。
            engine_options["poolclass"] = NullPool
        self.engine: AsyncEngine = create_async_engine(url, **engine_options)

        self.session_factory = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            # 提交事务后不让 ORM 对象立即失效，仍可直接访问对象属性
            autoflush=False,
            # 查询前不自动将当前会话中的修改刷新到数据库
        )

    async def create_all(self) -> None:
        """创建当前 metadata 中注册的全部数据表，主要用于测试环境。"""

        # 导入模型注册模块，使所有 ORM 模型加载并注册到 Base.metadata
        import app.bootstrap.model_registry  # noqa: F401

        async with self.engine.begin() as connection:
            # create_all 是同步方法，通过 run_sync 在异步连接中执行
            await connection.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        """删除当前 metadata 中注册的全部数据表，仅用于测试环境。"""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        """关闭数据库连接池并释放相关连接资源。"""

        await self.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """为每次 FastAPI 请求提供独立的异步数据库会话。"""

    # 数据库对象在应用启动阶段保存到了 app.state.database
    database: Database = request.app.state.database

    # 请求结束后自动关闭当前数据库会话
    async with database.session_factory() as session:
        try:
            yield session  # 将数据库会话注入路由函数或其他依赖
        except Exception:
            # 业务处理出现异常时回滚当前事务，避免保留未完成的数据修改
            await session.rollback()
            raise  # 继续向上抛出原始异常


# FastAPI 数据库会话依赖的类型别名
# 路由参数声明为 SessionDep 后，FastAPI 会自动调用 get_session
SessionDep = Annotated[AsyncSession, Depends(get_session)]

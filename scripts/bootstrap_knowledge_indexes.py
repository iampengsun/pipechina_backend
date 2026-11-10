"""显式创建指定代次的Elasticsearch、Qdrant和Neo4j知识索引结构。

该脚本用于首次部署或建立新索引代次，不会由API启动过程或Alembic自动执行。
"""

import argparse
import asyncio

from app.bootstrap.config import get_settings
from app.bootstrap.knowledge import KnowledgeResourceManager


async def initialize(generation: str) -> None:
    manager = KnowledgeResourceManager(get_settings())
    try:
        resources = manager.get()
        # Fail fast. Re-running is idempotent; publication is a separate business action.
        await resources.fulltext.initialize(generation)
        await resources.vector.initialize(generation)
        await resources.graph.initialize(generation)
    finally:
        await manager.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation", required=True)
    args = parser.parse_args()
    asyncio.run(initialize(args.generation))

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

# SQLite测试使用JSON，PostgreSQL运行环境自动切换为可索引的JSONB。
JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")

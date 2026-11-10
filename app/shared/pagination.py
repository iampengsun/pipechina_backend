import base64

# 将创建时间和主键编码成不透明游标，用于稳定的顺序分页。
import json
from datetime import datetime
from typing import Any


def encode_cursor(created_at: datetime, item_id: object) -> str:
    """把排序字段编码成不透明分页游标。"""

    raw = json.dumps([created_at.isoformat(), str(item_id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解析客户端提交的不透明分页游标。"""

    padding = "=" * (-len(cursor) % 4)
    values: list[Any] = json.loads(base64.urlsafe_b64decode(cursor + padding))
    return datetime.fromisoformat(values[0]), str(values[1])

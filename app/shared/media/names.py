import re
from pathlib import PurePath

# 清理用户提供的文件名，阻止路径穿越和控制字符进入对象键。


def safe_object_filename(filename: str) -> str:
    """返回适合存储的文件名，并移除路径穿越和控制字符。"""
    leaf = PurePath(filename.replace("\\", "/")).name
    sanitized = re.sub(r"[^0-9A-Za-z._-]+", "_", leaf).strip("._")
    return sanitized[:180] or "upload.bin"

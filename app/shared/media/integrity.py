"""文件摘要规范化工具。"""

from __future__ import annotations

import base64
import binascii
import re

_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def normalize_sha256(value: str | None) -> str | None:
    """把十六进制或S3使用的Base64 SHA-256转换为小写十六进制。"""
    if not value:
        return None
    candidate = value.strip().strip('"')
    if _SHA256_HEX.fullmatch(candidate):
        return candidate.lower()
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except ValueError, binascii.Error:
        return None
    return decoded.hex() if len(decoded) == 32 else None

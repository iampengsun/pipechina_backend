"""Cheap preflight, not a substitute for resource limits in the parser container."""

import zipfile
from pathlib import Path, PurePosixPath

OOXML_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word/document.xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xl/workbook.xml",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        "ppt/presentation.xml"
    ),
}


def validate_document(path: Path, mime_type: str, max_bytes: int) -> None:
    """在进入解析服务前校验文档类型和大小。"""

    size = path.stat().st_size
    if not 0 < size <= max_bytes:
        raise ValueError("document is empty or exceeds byte limit")
    with path.open("rb") as source:
        header = source.read(8192)
    if mime_type == "application/pdf":
        if not header.startswith(b"%PDF-"):
            raise ValueError("PDF signature mismatch")
    elif mime_type in OOXML_TYPES:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(e.file_size for e in entries) > max_bytes * 5:
                raise ValueError("expanded document exceeds limit")
            names = {e.filename for e in entries}
            if "[Content_Types].xml" not in names or OOXML_TYPES[mime_type] not in names:
                raise ValueError("Office document type mismatch")
            for entry in entries:
                parts = PurePosixPath(entry.filename)
                if (
                    parts.is_absolute()
                    or ".." in parts.parts
                    or "\\" in entry.filename
                    or entry.flag_bits & 1
                    or entry.filename.lower().endswith("vbaproject.bin")
                ):
                    raise ValueError("unsafe or unsupported Office archive")
    elif mime_type in {"text/plain", "text/markdown", "text/html", "text/csv"}:
        if b"\0" in header:
            raise ValueError("binary content declared as text")
    else:
        raise ValueError("unsupported document MIME type")

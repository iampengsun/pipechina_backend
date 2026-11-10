"""Bind release evidence to the exact deployable source, including service and migration code."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import hashlib

from scripts.manage_stack import ROOT


def source_digest():
    digest = hashlib.sha256()
    for folder in ["app", "services", "alembic", "scripts", "infra", ".github"]:
        for path in sorted((ROOT / folder).rglob("*")):
            if (
                not path.is_file()
                or path.suffix not in {".py", ".yml", ".txt"}
                and path.name != "Dockerfile"
            ):
                continue
            digest.update(str(path.relative_to(ROOT)).encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    for name in ["pyproject.toml", "uv.lock"]:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes() + b"\0")
    return digest.hexdigest()

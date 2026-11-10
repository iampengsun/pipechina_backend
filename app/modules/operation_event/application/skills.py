"""Versioned, repository-owned investigation skills loaded only after selection."""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from app.shared.errors import ConflictError

ROOT = Path(__file__).resolve().parents[1] / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    digest: str
    path: Path

    def summary(self):
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "digest": self.digest,
        }


def catalog():
    entries = {}
    for path in sorted(ROOT.glob("*/SKILL.md")):
        raw = path.read_text()
        if len(raw) > 24000 or not raw.startswith("---\n"):
            raise ConflictError("invalid investigation skill")
        parts = raw.split("---", 2)
        data = yaml.safe_load(parts[1])
        name = data["name"]
        version = data.get("metadata", {}).get("version")
        if (
            not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            or name != path.parent.name
            or not version
        ):
            raise ConflictError("invalid skill identity")
        entries[name] = Skill(
            name, data["description"], str(version), sha256(raw.encode()).hexdigest(), path
        )
    return entries


def load_selected(names, snapshots=None):
    available = catalog()
    result = []
    if len(names) > 3 or len(set(names)) != len(names):
        raise ConflictError("invalid skill selection")
    for name in names:
        skill = available.get(name)
        if skill is None:
            raise ConflictError("unknown investigation skill", skill=name)
        if snapshots is not None and snapshots.get(name) != skill.digest:
            raise ConflictError("skill changed; start a new investigation")
        result.append(
            {**skill.summary(), "instructions": skill.path.read_text().split("---", 2)[2].strip()}
        )
    return result

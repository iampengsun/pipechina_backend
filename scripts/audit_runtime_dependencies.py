"""Audit locked CPU inference dependencies against their upstream public package versions."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, Any] = {}
    passed = True
    for name in ["asr", "vision", "knowledge_runtime"]:
        lock = ROOT / "services" / name / "constraints.txt"
        # PyPI names upstream releases without the CPU wheel's local version suffix.
        requirements = lock.read_text().replace("+cpu", "")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt") as source:
            source.write(requirements)
            source.flush()
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "--no-deps",
                    "--disable-pip",
                    "-r",
                    source.name,
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
            )
        try:
            audit = json.loads(result.stdout)
        except ValueError:
            report[name] = {"status": "error", "detail": result.stderr[-2000:]}
            passed = False
            continue
        skipped = [d["name"] for d in audit["dependencies"] if "skip_reason" in d]
        vulnerabilities = [
            {"package": d["name"], "version": d["version"], "vulnerabilities": d["vulns"]}
            for d in audit["dependencies"]
            if d.get("vulns")
        ]
        okay = result.returncode == 0 and not skipped and not vulnerabilities
        report[name] = {
            "status": "passed" if okay else "failed",
            "packages": len(audit["dependencies"]),
            "skipped": skipped,
            "vulnerabilities": vulnerabilities,
        }
        passed &= okay
        print(name + ": " + report[name]["status"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

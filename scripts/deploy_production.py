"""Validate real production configuration and verified release gates before deployment."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from pydantic import ValidationError

from app.bootstrap.config import Settings
from scripts.manage_stack import ROOT
from scripts.release_manifest import source_digest


def validate(env_file, gate, restore):
    values = dotenv_values(env_file)
    selected = {key: value for key, value in values.items() if value is not None}
    try:
        with patch.dict(os.environ, selected, clear=True):
            settings = Settings(_env_file=env_file, app_env="production")
    except ValidationError as exc:
        problems = [
            ".".join(str(p) for p in error["loc"]) + ": " + error["msg"]
            for error in exc.errors(include_input=False)
        ]
        raise ValueError("; ".join(problems)) from None
    for key in [
        "POSTGRES_PASSWORD",
        "RABBITMQ_DEFAULT_PASS",
        "S3_SECRET_KEY",
        "ES_PASSWORD",
        "QDRANT_API_KEY",
        "NEO4J_PASSWORD",
        "KNOWLEDGE_SERVICE_API_KEY",
        "OPS_METRICS_TOKEN",
    ]:
        if len(values.get(key) or "") < 32:
            raise ValueError(key + " must have at least 32 characters")
    for key in ["JWT_ISSUER", "JWT_JWKS_URL"]:
        if "mock-idp" in (values.get(key) or ""):
            raise ValueError("development identity service cannot be used in production")
    gate_record = json.loads(gate.read_text())
    if gate_record.get("source_digest") != source_digest():
        raise ValueError("quality gate does not match current deployable source")
    if gate_record.get("status") != "passed":
        raise ValueError("expert quality gate has not passed")
    if json.loads(restore.read_text()).get("status") != "passed":
        raise ValueError("restore drill has not passed")
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--quality-gate", type=Path, required=True)
    parser.add_argument("--restore-report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        validate(args.env_file, args.quality_gate, args.restore_report)
    except (ValueError, OSError) as exc:
        raise SystemExit("Production validation blocked: " + str(exc)) from None
    command = [
        "docker",
        "compose",
        "--env-file",
        str(args.env_file.resolve()),
        "-f",
        str(ROOT / "infra/compose/project.yml"),
        "-f",
        str(ROOT / "infra/compose/production.yml"),
    ]
    environment = {
        **os.environ,
        **{key: value for key, value in dotenv_values(args.env_file).items() if value is not None},
        "PIPECHINA_PRODUCTION_ENV_FILE": str(args.env_file.resolve()),
    }
    subprocess.run(command + ["config", "--quiet"], env=environment, check=True)
    print("Production configuration and release gates validated")
    if args.apply:
        subprocess.run(
            command + ["up", "-d", "--build", "--wait", "--wait-timeout", "1800"],
            env=environment,
            cwd=ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()

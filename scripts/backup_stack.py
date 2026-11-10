"""Consistent project backup and isolated database/index/object query recovery drill."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.engine import make_url

from app.bootstrap.config import get_settings
from scripts.manage_stack import ROOT

COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    str(ROOT / ".env"),
    "-f",
    str(ROOT / "infra/compose/project.yml"),
]
CACHES = {"asr_models", "vision_models", "knowledge_runtime_models", "knowledge_runtime_ocr"}


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def captured(args):
    return run(args, capture_output=True, text=True).stdout


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fingerprints(container, username, database):
    names = captured(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            username,
            "-d",
            database,
            "-At",
            "-c",
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename",
        ]
    ).splitlines()
    if any(not re.fullmatch(r"[a-zA-Z0-9_]+", name) for name in names):
        raise RuntimeError("unexpected table name")
    sql = (
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"
        + ";".join(
            f"SELECT '{name}', count(*), "
            f"md5(coalesce(string_agg(row_to_json(t)::text,'' "
            f"ORDER BY row_to_json(t)::text),'')) FROM \"{name}\" t"
            for name in names
        )
        + ";COMMIT;"
    )
    rows = captured(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            username,
            "-d",
            database,
            "-qAt",
            "-F",
            "|",
            "-c",
            sql,
        ]
    )
    return {
        parts[0]: {"rows": int(parts[1]), "digest": parts[2]}
        for line in rows.splitlines()
        if len(parts := line.split("|")) == 3
    }


RECOVERY_SERVICES = ["elasticsearch", "qdrant", "neo4j", "seaweedfs"]


def probe_service(container, service, environment, project):
    response = run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "container:" + container,
            "--label",
            "com.docker.compose.project=" + project,
            "--entrypoint",
            "python",
            "pipechina-backend:dev",
            "-c",
            (ROOT / "scripts/restore_probe.py").read_text(),
        ],
        input=json.dumps({"service": service, "environment": environment}),
        capture_output=True,
        text=True,
    )
    return json.loads(response.stdout)


def capture_recovery_services(config, project):
    runtime, checks = {}, {}
    volume_names = {value["name"]: key for key, value in config["volumes"].items()}
    for service in RECOVERY_SERVICES:
        container = captured(COMPOSE + ["ps", "-q", service]).strip()
        if not container:
            raise RuntimeError("recovery source is not running: " + service)
        row = json.loads(captured(["docker", "inspect", container]))[0]
        runtime[service] = {
            "image": row["Image"],
            "environment": row["Config"]["Env"],
            "hostname": row["Config"]["Hostname"],
            "command": row["Config"]["Cmd"],
            "mounts": [
                {"key": volume_names[m["Name"]], "destination": m["Destination"]}
                for m in row["Mounts"]
                if m["Type"] == "volume" and m["Name"] in volume_names
            ],
        }
        checks[service] = probe_service(container, service, row["Config"]["Env"], project)
    return runtime, checks


def verify_recovery_services(source, manifest, project):
    runtime = json.loads((source / "runtime-private.json").read_text())
    verified = {}
    network = project + "-internal"
    run(
        [
            "docker",
            "network",
            "create",
            "--internal",
            "--label",
            "com.docker.compose.project=" + project,
            network,
        ],
        stdout=subprocess.DEVNULL,
    )
    try:
        for service in RECOVERY_SERVICES:
            definition = runtime[service]
            container = project + "-" + service
            with tempfile.NamedTemporaryFile(
                mode="w", prefix="pipechina-restore-env-", delete=True
            ) as envfile:
                if any("\n" in item for item in definition["environment"]):
                    raise RuntimeError("multiline runtime environment is unsupported")
                envfile.write("\n".join(definition["environment"]) + "\n")
                envfile.flush()
                command = [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container,
                    "--network",
                    network,
                    "--hostname",
                    definition["hostname"],
                    "--security-opt",
                    "no-new-privileges:true",
                    "--env-file",
                    envfile.name,
                    "--label",
                    "com.docker.compose.project=" + project,
                ]
                for mount in definition["mounts"]:
                    if mount["key"] not in manifest["volumes"]:
                        raise RuntimeError("required recovery volume missing")
                    command += [
                        "--mount",
                        f"type=volume,source={project}_{mount['key']},target={mount['destination']}",
                    ]
                command += [definition["image"], *(definition["command"] or [])]
                try:
                    run(command, stdout=subprocess.DEVNULL)
                    deadline = time.monotonic() + 150
                    while True:
                        try:
                            actual = probe_service(
                                container, service, definition["environment"], project
                            )
                            break
                        except subprocess.CalledProcessError, ValueError:
                            if time.monotonic() >= deadline:
                                raise RuntimeError(
                                    "restored service did not become queryable: " + service
                                ) from None
                            time.sleep(2)
                    if actual != manifest["service_probes"][service]:
                        raise RuntimeError("restored service content differs: " + service)
                    verified[service] = "query_verified_against_source_snapshot"
                finally:
                    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    finally:
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
    return verified


def backup(destination):
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o700)
    config = json.loads(captured(COMPOSE + ["config", "--format", "json"]))
    if config["name"] != "pipechina":
        raise RuntimeError("only the pipechina project is supported")
    running = captured(COMPOSE + ["ps", "--status", "running", "--services"]).split()
    postgres = captured(COMPOSE + ["ps", "-q", "postgres"]).strip()
    address = make_url(get_settings().database_url)
    if not postgres or not address.username or not address.database:
        raise RuntimeError("project PostgreSQL must be running")
    manifest: dict[str, Any] = {
        "format": 1,
        "project": "pipechina",
        "created_at": datetime.now(UTC).isoformat(),
        "volumes": {},
        "excluded_caches": sorted(CACHES),
        "status": "incomplete",
    }
    # Stop writers before checking data, then freeze all volume owners for a consistent snapshot.
    writers = [
        name
        for name in running
        if name in {"api", "worker", "maintenance-worker", "beat", "knowledge-runtime"}
    ]
    try:
        if writers:
            run(COMPOSE + ["stop", "--timeout", "120", *writers], stdout=subprocess.DEVNULL)
        manifest["tables"] = fingerprints(postgres, address.username, address.database)
        runtime, manifest["service_probes"] = capture_recovery_services(config, "pipechina")
        (destination / "runtime-private.json").write_text(json.dumps(runtime))
        manifest["postgres"] = {
            "username": address.username,
            "database": address.database,
            "image": config["services"]["postgres"]["image"],
        }
        run(COMPOSE + ["stop", "--timeout", "120"], stdout=subprocess.DEVNULL)
        for key, volume in config["volumes"].items():
            if key in CACHES:
                continue
            filename = key + ".tar.gz"
            target = destination / filename
            with target.open("wb") as stream:
                run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--user",
                        "0:0",
                        "--label",
                        "com.docker.compose.project=pipechina",
                        "--mount",
                        f"type=volume,source={volume['name']},target=/source,readonly",
                        "--entrypoint",
                        "tar",
                        "pipechina-backend:dev",
                        "-czpf",
                        "-",
                        "-C",
                        "/source",
                        ".",
                    ],
                    stdout=stream,
                )
            manifest["volumes"][key] = {
                "archive": filename,
                "sha256": digest(target),
                "bytes": target.stat().st_size,
            }
        manifest["status"] = "complete"
    finally:
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if running:
            run(COMPOSE + ["up", "-d", "--no-deps", *running], stdout=subprocess.DEVNULL)
    print(
        json.dumps(
            {
                "backup": str(destination),
                "status": manifest["status"],
                "volumes": len(manifest["volumes"]),
            }
        )
    )


def drill(source, output):
    manifest = json.loads((source / "manifest.json").read_text())
    if (
        manifest.get("format") != 1
        or manifest.get("project") != "pipechina"
        or manifest.get("status") != "complete"
    ):
        raise RuntimeError("backup is incomplete or from another project")
    project = "pipechina-restore-" + uuid4().hex[:10]
    volumes = []
    container = project + "-postgres"
    result: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "project": project,
        "status": "failed",
        "volumes": {},
    }
    try:
        for key, item in manifest["volumes"].items():
            if not re.fullmatch(r"[a-z0-9_]+", key) or item["archive"] != key + ".tar.gz":
                raise RuntimeError("unexpected archive path")
            archive = source / item["archive"]
            if digest(archive) != item["sha256"]:
                raise RuntimeError("backup checksum mismatch")
            name = project + "_" + key
            run(
                [
                    "docker",
                    "volume",
                    "create",
                    "--label",
                    f"com.docker.compose.project={project}",
                    name,
                ],
                stdout=subprocess.DEVNULL,
            )
            volumes.append(name)
            with archive.open("rb") as stream:
                run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "-i",
                        "--network",
                        "none",
                        "--read-only",
                        "--user",
                        "0:0",
                        "--label",
                        f"com.docker.compose.project={project}",
                        "--mount",
                        f"type=volume,source={name},target=/target",
                        "--entrypoint",
                        "tar",
                        "pipechina-backend:dev",
                        "-xzpf",
                        "-",
                        "-C",
                        "/target",
                    ],
                    stdin=stream,
                )
            # Compare restored files with the original archive before any server changes files.
            with archive.open("rb") as stream:
                run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "-i",
                        "--network",
                        "none",
                        "--read-only",
                        "--user",
                        "0:0",
                        "--mount",
                        f"type=volume,source={name},target=/target,readonly",
                        "--entrypoint",
                        "tar",
                        "pipechina-backend:dev",
                        "-dzpf",
                        "-",
                        "-C",
                        "/target",
                    ],
                    stdin=stream,
                    stdout=subprocess.DEVNULL,
                )
            result["volumes"][key] = "restored_and_compared"
        pg = manifest["postgres"]
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--network",
                "none",
                "--label",
                f"com.docker.compose.project={project}",
                "--mount",
                f"type=volume,source={project}_pg_data,target=/var/lib/postgresql",
                "-e",
                "POSTGRES_PASSWORD=" + secrets.token_hex(24),
                pg["image"],
            ],
            stdout=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", pg["username"]],
                capture_output=True,
            )
            if probe.returncode == 0:
                break
            time.sleep(1)
        actual = fingerprints(container, pg["username"], pg["database"])
        if actual != manifest["tables"]:
            raise RuntimeError("restored PostgreSQL records differ from snapshot")
        if "service_probes" in manifest:
            result["service_recovery"] = verify_recovery_services(source, manifest, project)
        result.update(
            status="passed",
            verified_tables=len(actual),
            index_verification="query_verified"
            if "service_recovery" in result
            else "archive file comparison; query-level recovery not claimed",
        )
    except Exception as exc:
        result["failure"] = type(exc).__name__
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        for name in volumes:
            subprocess.run(["docker", "volume", "rm", name], capture_output=True)
        result["finished_at"] = datetime.now(UTC).isoformat()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"drill": result["status"], "tables": result["verified_tables"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    b = sub.add_parser("backup")
    b.add_argument("directory", type=Path)
    d = sub.add_parser("drill")
    d.add_argument("directory", type=Path)
    d.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "backup":
        backup(args.directory.resolve())
    else:
        drill(args.directory.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

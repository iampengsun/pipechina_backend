"""Refresh a project runtime from its reviewed requirements without downloading a new base image."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import json
import subprocess
from uuid import uuid4

from scripts.manage_stack import ROOT


def run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr)[-6000:])
    return result.stdout.strip()


def install(component):
    image = "pipechina-" + component + (":dev" if component == "backend" else ":latest")
    name = "pipechina-runtime-" + uuid4().hex[:10]
    uv_runtime = component in {"backend", "mock-idp"}
    folder = "knowledge_runtime" if component == "knowledge-runtime" else component
    requirements = ROOT / "services" / folder / "requirements.txt"
    constraints = requirements.with_name("constraints.txt")
    config = json.loads(run(["docker", "image", "inspect", image]))[0]["Config"]
    try:
        run(
            [
                "docker",
                "create",
                "--name",
                name,
                "--network",
                "pipechina_default",
                "--user",
                "0:0",
                "--label",
                "com.docker.compose.project=pipechina",
                image,
                *(
                    ["uv", "sync", "--frozen", "--no-dev", "--no-editable"]
                    + (["--no-install-project"] if component == "mock-idp" else [])
                    if uv_runtime
                    else [
                        "python",
                        "-m",
                        "pip",
                        "install",
                        "--no-cache-dir",
                        "-r",
                        "/service/requirements.txt",
                        "--constraint",
                        "/service/constraints.txt",
                    ]
                ),
            ]
        )
        if uv_runtime:
            for filename in ["pyproject.toml", "uv.lock", "README.md"]:
                run(["docker", "cp", str(ROOT / filename), name + ":/app/" + filename])
        else:
            run(["docker", "cp", str(requirements), name + ":/service/requirements.txt"])
        if not uv_runtime and constraints.exists():
            run(["docker", "cp", str(constraints), name + ":/service/constraints.txt"])
        run(["docker", "start", "-a", name])
        if run(["docker", "inspect", "--format", "{{.State.ExitCode}}", name]) != "0":
            raise RuntimeError("dependency installation failed")
        run(
            [
                "docker",
                "commit",
                "--change",
                "USER " + config["User"],
                "--change",
                "CMD " + json.dumps(config["Cmd"]),
                name,
                image,
            ]
        )
        print(component + ": reviewed requirements installed")
    finally:
        subprocess.run(["docker", "rm", name], capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "component", choices=["backend", "mock-idp", "asr", "knowledge-runtime", "vision"]
    )
    install(parser.parse_args().component)

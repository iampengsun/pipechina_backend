"""Rebuild code from project-owned local runtimes only when dependency locks match exactly."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from scripts.manage_stack import ROOT

COMPONENTS = [
    (
        "pipechina-knowledge-runtime:latest",
        "services/knowledge_runtime/requirements.txt",
        "/service/requirements.txt",
        [
            ("services/knowledge_runtime/main.py", "/service/main.py"),
            ("services/knowledge_runtime/convert.py", "/service/convert.py"),
        ],
    ),
    (
        "pipechina-backend:dev",
        "uv.lock",
        "/app/uv.lock",
        [
            ("app", "/app/app"),
            ("app", "/app/.venv/lib/python3.14/site-packages/app"),
            ("scripts", "/app/scripts"),
            ("alembic", "/app/alembic"),
            ("alembic.ini", "/app/alembic.ini"),
        ],
    ),
    (
        "pipechina-mock-idp:latest",
        "uv.lock",
        "/app/uv.lock",
        [("dev/mock_idp", "/app/dev/mock_idp")],
    ),
    (
        "pipechina-asr:latest",
        "services/asr/requirements.txt",
        "/service/requirements.txt",
        [
            ("services/asr/app.py", "/service/app.py"),
            ("services/asr/diarization.py", "/service/diarization.py"),
        ],
    ),
    (
        "pipechina-vision:latest",
        "services/vision/requirements.txt",
        "/service/requirements.txt",
        [
            ("services/vision/app.py", "/service/app.py"),
            ("services/vision/token_output.py", "/service/token_output.py"),
        ],
    ),
]


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def build(selected=None):
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix="pipechina-build-") as temp:
            for image, lock, remote, files in COMPONENTS:
                if selected and image.split(":")[0].removeprefix("pipechina-") not in selected:
                    continue
                name = "pipechina-build-" + uuid4().hex[:10]
                config = json.loads(run(["docker", "image", "inspect", image]))[0]["Config"]
                if config.get("Entrypoint"):
                    raise RuntimeError(image + ": custom entrypoints require a Dockerfile rebuild")
                directories = [remote for local, remote in files if (ROOT / local).is_dir()]
                cleanup = (
                    "from pathlib import Path\n"
                    "import shutil\n"
                    f"for value in {directories!r}:\n"
                    "    path = Path(value)\n"
                    "    if path.exists():\n"
                    "        shutil.rmtree(path)\n"
                )
                run(
                    [
                        "docker",
                        "create",
                        "--name",
                        name,
                        "--label",
                        "com.docker.compose.project=pipechina",
                        "--network",
                        "none",
                        "--user",
                        "0:0",
                        image,
                        "python",
                        "-c",
                        cleanup,
                    ]
                )
                created.append((name, image, files, config, directories))
                target = Path(temp) / str(len(created))
                run(["docker", "cp", name + ":" + remote, str(target)])
                constraints = (ROOT / lock).with_name("constraints.txt")
                if constraints.exists():
                    frozen = Path(temp) / (str(len(created)) + "-constraints")
                    try:
                        run(["docker", "cp", name + ":/service/constraints.txt", str(frozen)])
                    except subprocess.CalledProcessError:
                        raise RuntimeError(
                            image + ": runtime constraints are not installed"
                        ) from None
                    if frozen.read_bytes() != constraints.read_bytes():
                        raise RuntimeError(image + ": runtime constraints differ; rebuild required")
                if target.read_bytes() != (ROOT / lock).read_bytes():
                    raise RuntimeError(
                        image + ": dependency lock differs; online runtime rebuild required"
                    )
            for name, image, files, config, directories in created:
                if directories:
                    run(["docker", "start", "-a", name])
                    if run(["docker", "inspect", "--format", "{{.State.ExitCode}}", name]) != "0":
                        raise RuntimeError("source cleanup failed: " + image)
                for index, (local, remote) in enumerate(files):
                    path = ROOT / local
                    if path.is_dir():
                        staging = Path(temp) / (name + "-" + str(index))
                        shutil.copytree(
                            path, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                        )
                        run(["docker", "cp", str(staging), name + ":" + remote])
                    else:
                        run(["docker", "cp", str(path), name + ":" + remote])
                run(
                    [
                        "docker",
                        "commit",
                        "--change",
                        "LABEL pipechina.build_method=verified-local-runtime",
                        "--change",
                        "USER " + (config.get("User") or "0"),
                        "--change",
                        "CMD " + json.dumps(config.get("Cmd") or []),
                        name,
                        image,
                    ]
                )
        digest = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
        print(
            json.dumps(
                {
                    "method": "verified_local_runtime",
                    "images": [c[1] for c in created],
                    "uv_lock_sha256": digest,
                }
            )
        )
    finally:
        for name, *_ in created:
            subprocess.run(["docker", "rm", name], capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--components",
        nargs="+",
        choices=["backend", "mock-idp", "asr", "vision", "knowledge-runtime"],
    )
    build(parser.parse_args().components)

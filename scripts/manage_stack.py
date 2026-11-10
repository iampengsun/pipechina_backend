"""One command for the isolated project stack; reset only this Compose project's data."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def configure():
    path = ROOT / ".env"
    if not path.exists():
        path.write_text((ROOT / ".env.example").read_text())
        path.chmod(0o600)
    content = path.read_text()
    lines = content.splitlines()
    keys = (
        "ES_PASSWORD",
        "QDRANT_API_KEY",
        "NEO4J_PASSWORD",
        "KNOWLEDGE_SERVICE_API_KEY",
        "OPS_METRICS_TOKEN",
    )
    for key in keys:
        found = next((i for i, line in enumerate(lines) if line.startswith(key + "=")), None)
        if found is None:
            lines.append(f"{key}={secrets.token_hex(24)}")
        elif not lines[found].split("=", 1)[1].strip():
            lines[found] = f"{key}={secrets.token_hex(24)}"
    path.write_text("\n".join(lines) + "\n")


def validate_text_configuration():
    values = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    provider = values.get("TEXT_PROVIDER", "qwen")
    required = {"qwen": "DASHSCOPE_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
    if required and not values.get(required):
        raise SystemExit(
            f"请先在 .env 填写 {required}；当前 TEXT_PROVIDER={provider}。未启动或删除任何容器。"
        )


def validate_runtime_capacity():
    result = subprocess.run(
        ["docker", "info", "--format", "{{.MemTotal}}"], capture_output=True, text=True
    )
    if result.returncode:
        raise SystemExit("请先启动Docker，再启动本项目。未启动或删除任何容器。")
    if int(result.stdout.strip()) < 15 * 1024**3:
        raise SystemExit(
            "完整CPU模型栈需要给Docker分配至少16GB内存；请在Docker资源设置中调整后重试。"
            "未启动或删除任何容器。"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["up", "down", "status", "logs", "reset"])
    parser.add_argument(
        "--offline", action="store_true", help="reuse local runtimes only if dependency locks match"
    )
    args = parser.parse_args()
    if args.action in {"up", "reset"}:
        configure()
        validate_text_configuration()
        validate_runtime_capacity()
    elif not (ROOT / ".env").is_file():
        raise SystemExit("项目尚未配置，请先运行 up 生成 .env 并填写模型密钥。")
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / ".env"),
        "-f",
        str(ROOT / "infra/compose/project.yml"),
    ]
    if args.offline:
        if args.action != "up":
            raise SystemExit("--offline supports only up")
        from scripts.build_offline import build

        build()
    commands = {
        "up": ["up", "-d", "--build", "--remove-orphans", "--wait", "--wait-timeout", "1800"],
        "down": ["down", "--remove-orphans"],
        "status": ["ps", "-a"],
        "logs": ["logs", "--tail=100"],
        "reset": ["down", "--volumes", "--remove-orphans"],
    }
    if args.offline:
        commands["up"].remove("--build")
        commands["up"].append("--no-build")
    subprocess.run(compose + commands[args.action], cwd=ROOT, check=True)
    if args.action == "reset":
        subprocess.run(compose + commands["up"], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()

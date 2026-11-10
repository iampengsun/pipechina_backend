"""Independent real-service acceptance scenarios; failures never suppress unrelated scenarios."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.bootstrap.config import get_settings
from scripts.acceptance import run as run_ai


class Failure(Exception):
    pass


class Blocked(Exception):
    pass


class Suite:
    def __init__(self, args):
        self.args = args
        self.resources = {}
        self.headers = {}
        self.reviewer = {}
        self.client = httpx.AsyncClient(timeout=args.timeout, trust_env=False)

    async def call(self, method, path, payload=None, *, reviewer=False):
        response = await self.client.request(
            method,
            self.args.api_url + path,
            json=payload,
            headers=self.reviewer if reviewer else self.headers,
        )
        if not response.is_success:
            try:
                code = response.json().get("code", "HTTP_ERROR")
            except ValueError:
                code = "HTTP_ERROR"
            raise Failure(f"{method} {path}: {response.status_code} {code}")
        return response.json()

    async def login(self):
        if self.args.development_login:
            if any(
                urlsplit(url).hostname not in {"127.0.0.1", "localhost"}
                for url in [self.args.api_url, self.args.idp_url]
            ):
                raise Failure("development login requires localhost")
            for username, destination in [("admin", self.headers), ("acceptance", self.reviewer)]:
                r = await self.client.post(
                    self.args.idp_url + "/token",
                    data={"username": username, "password": "123456", "grant_type": "password"},
                )
                if not r.is_success:
                    raise Failure("development login failed")
                destination["Authorization"] = "Bearer " + r.json()["access_token"]
        else:
            self.headers = {
                "Authorization": "Bearer " + os.environ.get("PIPECHINA_ACCESS_TOKEN", "")
            }
            self.reviewer = {
                "Authorization": "Bearer " + os.environ.get("PIPECHINA_REVIEWER_TOKEN", "")
            }
        self.me = await self.call("GET", "/api/v1/auth/me")
        reviewer = await self.call("GET", "/api/v1/auth/me", reviewer=True)
        if self.me["user_id"] == reviewer["user_id"]:
            raise Failure("acceptance needs an independent reviewer")

    def measurement(self):
        return {
            "metric": "合成测试读数",
            "unit": "test-unit",
            "before": 2,
            "after": 0,
            "maximum": 0.1,
            "criterion": "软件合成测试计划，不能用于现场安全验收",
            "measured_at": datetime.now(UTC).isoformat(),
        }

    async def wait(self, job_id):
        for _ in range(max(1, self.args.timeout // 2)):
            job = await self.call("GET", "/api/v1/jobs/" + str(job_id))
            if job["status"] == "succeeded":
                return job
            if job["status"] in {"failed", "cancelled"}:
                raise Failure(f"job failed: {job.get('error_code')}")
            await asyncio.sleep(2)
        raise Failure("job timeout")

    async def hazard(self):
        row = await self.call(
            "POST",
            "/api/v1/hazards",
            {
                "title": "[合成验收] 实测闭环 " + uuid4().hex[:8],
                "description": "仅软件测试，不指示现场动作",
                "location": "合成测试站场",
                "category": "合成测试",
                "source_type": "manual",
            },
        )
        root = "/api/v1/hazards/" + row["id"]
        self.resources["hazard_id"] = row["id"]
        row = await self.call(
            "POST",
            root + ":assess",
            {
                "expected_version": row["version"],
                "likelihood": 1,
                "consequence": 1,
                "rationale": "合成风险数据",
                "control_measures": "只测试软件状态流转",
                "assignee_id": self.me["user_id"],
                "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        row = await self.call(
            "POST",
            root + ":start-rectification",
            {"expected_version": row["version"], "reason": "合成记录"},
        )
        row = await self.call(
            "POST",
            root + ":submit-rectification",
            {
                "expected_version": row["version"],
                "action": "登记合成措施",
                "result": "合成复测通过",
                "measurements": [self.measurement()],
            },
        )
        history = await self.call("GET", root)
        rectification = history["rectifications"][-1]
        row = await self.call(
            "POST",
            root + ":accept",
            {
                "expected_version": row["version"],
                "reason": "独立复核合成软件测试",
                "rectification_id": rectification["id"],
                "measurements": rectification["measurements"],
            },
            reviewer=True,
        )
        if row["status"] != "closed":
            raise Failure("hazard did not close")
        return {"hazard_id": row["id"], "independent_acceptance": True}

    async def meeting(self):
        row = await self.call(
            "POST",
            "/api/v1/meetings",
            {
                "title": "[合成验收] ACL调整",
                "started_at": datetime.now(UTC).isoformat(),
                "filename": "synthetic.txt",
                "mime_type": "audio/wav",
                "size_bytes": 4,
            },
        )
        root = "/api/v1/meetings/" + row["meeting"]["id"]
        acl = await self.call(
            "PUT",
            root + "/acl",
            {
                "expected_version": row["meeting"]["acl_version"],
                "reader_ids": [self.me["user_id"]],
                "reason": "合成访问范围调整",
            },
        )
        if acl["acl_version"] != row["meeting"]["acl_version"] + 1:
            raise Failure("ACL version not advanced")
        return {"meeting_id": row["meeting"]["id"], "acl_version": acl["acl_version"]}

    async def ai(self):
        report = self.args.output.with_name(self.args.output.stem + "-ai.json")
        try:
            await run_ai(
                SimpleNamespace(
                    api_url=self.args.api_url,
                    idp_url=self.args.idp_url,
                    token_env="PIPECHINA_ACCESS_TOKEN",
                    development_login=self.args.development_login,
                    timeout=self.args.timeout,
                    output=report,
                )
            )
        finally:
            if report.exists():
                result = json.loads(report.read_text())
                if result.get("status") == "passed":
                    self.resources.update(result["resources"])
        return {"report": str(report)}

    async def work_order(self):
        key = self.resources.get("work_order_id")
        if key is None and self.args.seed_synthetic:
            if urlsplit(self.args.api_url).hostname not in {"127.0.0.1", "localhost"}:
                raise Blocked("synthetic fixtures require a local API")
            from dev.acceptance_fixtures import create_synthetic_order

            fixture = await create_synthetic_order(self.me["user_id"])
            self.resources.update(fixture)
            await self.call(
                "POST",
                f"/api/v1/events/{fixture['event_id']}:confirm",
                {"expected_version_id": fixture["event_version_id"]},
            )
            key = fixture["work_order_id"]
        if key is None:
            raise Blocked(
                "requires a successful AI scenario, or "
                "explicit --seed-synthetic for local development"
            )
        root = "/api/v1/work-orders/" + key
        row = await self.call("GET", root)
        for action, extra in [
            ("submit-review", {}),
            ("review", {"approved": True}),
            (
                "dispatch",
                {
                    "assignee_id": self.me["user_id"],
                    "due_at": (datetime.now(UTC) + timedelta(seconds=3)).isoformat(),
                },
            ),
            ("start", {}),
            (
                "resolve",
                {
                    "action": "登记合成测试措施",
                    "result": "合成读数通过",
                    "measurements": [self.measurement()],
                },
            ),
        ]:
            if action == "dispatch":
                extra["due_at"] = (datetime.now(UTC) + timedelta(seconds=3)).isoformat()
            if action == "resolve":
                for _ in range(45):
                    reminders = await self.call(
                        "GET", "/api/v1/work-order-reminders?status=pending"
                    )
                    reminder = next((r for r in reminders if r["work_order_id"] == key), None)
                    if reminder:
                        await self.call(
                            "POST",
                            f"/api/v1/work-order-reminders/{reminder['id']}:acknowledge",
                            {
                                "expected_version": reminder["version"],
                                "reason": "合成测试领取逾期处理",
                            },
                        )
                        self.resources["reminder_id"] = reminder["id"]
                        break
                    await asyncio.sleep(2)
                else:
                    raise Failure("overdue reminder was not generated")
                extra["measurements"] = [self.measurement()]
            row = await self.call(
                "POST",
                root + ":" + action,
                {
                    "expected_version": row["version"],
                    "reason": "仅软件合成验收，不授权现场动作",
                    **extra,
                },
            )
        history = await self.call("GET", root + "/resolutions")
        row = await self.call(
            "POST",
            root + ":close",
            {
                "expected_version": row["version"],
                "resolution_id": history[-1]["id"],
                "measurements": history[-1]["measurements"],
                "approved": True,
                "reason": "独立核对合成数据",
            },
            reviewer=True,
        )
        if row["status"] != "closed":
            raise Failure("work order not closed")
        return {"work_order_id": key, "independent_acceptance": True}

    async def memory(self):
        key = self.resources.get("work_order_id")
        if key is None:
            raise Blocked("requires a closed synthetic work order")
        row = await self.call("GET", "/api/v1/work-orders/" + key)
        if row["status"] != "closed":
            raise Blocked("synthetic order has not been independently accepted")
        job = await self.call(
            "POST",
            f"/api/v1/work-orders/{key}:consolidate-memory",
            {"expected_version": row["version"]},
        )
        await self.wait(job["job_id"])
        memories = await self.call("GET", "/api/v1/experiences")
        memory = next(
            (item for item in memories if str(item.get("job_id")) == str(job["job_id"])), None
        )
        if memory is None:
            raise Failure("memory result not found")
        memory = await self.call(
            "POST",
            f"/api/v1/experiences/{memory['id']}:review",
            {
                "expected_version": memory["version"],
                "approved": True,
                "reason": "合成经验的软件验收，不作为现场知识",
            },
            reviewer=True,
        )
        await self.call(
            "POST",
            f"/api/v1/experiences/{memory['id']}:withdraw",
            {"expected_version": memory["version"], "reason": "合成测试结束，不作为现场经验"},
        )
        return {
            "memory_id": memory["id"],
            "independently_approved": True,
            "final_status": "withdrawn",
        }

    async def asr(self):
        if not self.args.audio:
            raise Blocked("provide --audio synthetic fixture")
        with self.args.audio.open("rb") as audio:
            r = await self.client.post(
                self.args.asr_url + "/v1/transcriptions",
                files={"file": (self.args.audio.name, audio, "audio/wav")},
                data={"model": get_settings().asr_model, "language": "en"},
            )
        if not r.is_success or not r.json().get("full_text"):
            raise Failure(f"real ASR failed: HTTP {r.status_code}")
        transcript = r.json()
        labels = {
            item.get("speaker_label")
            for item in transcript["segments"]
            if item.get("speaker_label")
        }
        if len(labels) != self.args.expected_speakers:
            raise Failure(
                f"speaker count mismatch: expected {self.args.expected_speakers}, got {len(labels)}"
            )
        return {"transcript": transcript, "expert_validated": False}

    async def vision(self):
        if not self.args.image:
            raise Blocked("provide --image synthetic fixture")
        with self.args.image.open("rb") as image:
            r = await self.client.post(
                self.args.vision_url + "/v1/vision/analyze",
                files={"file": (self.args.image.name, image, "image/png")},
                data={
                    "model": get_settings().vision_model,
                    "context": "合成图像，仅检验调用，不推断现场事实",
                },
            )
        if not r.is_success or not isinstance(r.json().get("findings"), list):
            raise Failure(f"real vision failed: HTTP {r.status_code}")
        if (
            self.args.expected_findings is not None
            and len(r.json()["findings"]) != self.args.expected_findings
        ):
            raise Failure(
                f"finding count mismatch: expected {self.args.expected_findings}, "
                f"got {len(r.json()['findings'])}"
            )
        return {"vision": r.json(), "expert_validated": False}

    async def media(self):
        return {"asr": await self.asr(), "vision": await self.vision()}

    async def documents(self):
        if not self.args.document_manifest:
            raise Blocked("provide --document-manifest containing files and expected text")
        fixtures = json.loads(self.args.document_manifest.read_text())
        settings = get_settings()
        if settings.knowledge_service_api_key is None:
            raise Blocked("knowledge runtime service credential missing")
        headers = {
            "Authorization": "Bearer " + settings.knowledge_service_api_key.get_secret_value()
        }
        results = []
        for fixture in fixtures["cases"]:
            file = (self.args.document_manifest.parent / fixture["file"]).resolve()
            with file.open("rb") as stream:
                r = await self.client.post(
                    settings.knowledge_runtime_url + "/v1/parse-jobs",
                    headers={**headers, "Idempotency-Key": "acceptance-" + uuid4().hex},
                    files={"file": (file.name, stream, fixture["mime_type"])},
                    data={"max_pages": 20},
                )
            if not r.is_success:
                raise Failure(f"document submit failed: {r.status_code}")
            key = r.json()["id"]
            for _ in range(max(1, self.args.timeout // 2)):
                r = await self.client.get(
                    settings.knowledge_runtime_url + "/v1/parse-jobs/" + key, headers=headers
                )
                body = r.json()
                if body.get("status") in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(2)
            blocks = body.get("blocks", [])
            text = " ".join(block["text"] for block in blocks)
            missing = [
                term
                for term in fixture["expected_terms"]
                if "".join(term.split()) not in "".join(text.split())
            ]
            if body.get("status") != "succeeded" or missing:
                raise Failure(
                    f"document {fixture['case_id']}: job={key}, status={body.get('status')}, "
                    f"blocks={len(blocks)}, missing={missing}"
                )
            results.append({"case_id": fixture["case_id"], "blocks": len(blocks), "job_id": key})
        return {"cases": results, "expert_validated": False}

    async def reminders(self):
        key = self.resources.get("reminder_id")
        if key is None:
            raise Blocked(
                "requires the work_order scenario to generate and acknowledge an overdue reminder"
            )
        rows = await self.call("GET", "/api/v1/work-order-reminders?status=resolved")
        if not any(r["id"] == key and r["acknowledged_by"] for r in rows):
            raise Failure("acknowledged reminder was not resolved after closure")
        return {"reminder_id": key, "status": "resolved", "acknowledgement_preserved": True}


async def run(args):
    suite = Suite(args)
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "provenance": "synthetic_acceptance",
        "expert_validated": False,
        "cases": [],
        "resources": suite.resources,
    }
    try:
        await suite.login()
        for name in args.cases:
            result = {"name": name, "started_at": datetime.now(UTC).isoformat()}
            try:
                async with asyncio.timeout(args.timeout):
                    value = await getattr(suite, name)()
                result.update(status="passed", result=value)
            except Blocked as exc:
                result.update(status="blocked", reason=str(exc))
            except Exception as exc:
                result.update(
                    status="failed",
                    reason=str(exc) if isinstance(exc, Failure) else type(exc).__name__,
                )
            result["finished_at"] = datetime.now(UTC).isoformat()
            report["cases"].append(result)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(name + ": " + result["status"], flush=True)
    finally:
        await suite.client.aclose()
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["status"] = (
            "passed"
            if report["cases"] and all(c["status"] == "passed" for c in report["cases"])
            else "incomplete"
        )
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report["status"] == "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--idp-url", default="http://127.0.0.1:9001")
    parser.add_argument("--asr-url", default="http://127.0.0.1:8101")
    parser.add_argument("--vision-url", default="http://127.0.0.1:8102")
    parser.add_argument("--development-login", action="store_true")
    parser.add_argument(
        "--seed-synthetic",
        action="store_true",
        help="local administrator-only manual fixtures; does not test AI generation",
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=[
            "hazard",
            "meeting",
            "ai",
            "work_order",
            "memory",
            "media",
            "asr",
            "vision",
            "documents",
            "reminders",
        ],
        default=[
            "hazard",
            "meeting",
            "ai",
            "work_order",
            "memory",
            "asr",
            "vision",
            "documents",
            "reminders",
        ],
    )
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--expected-findings", type=int)
    parser.add_argument("--expected-speakers", type=int, default=2)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--document-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()

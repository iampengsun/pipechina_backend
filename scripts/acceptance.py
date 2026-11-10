"""Real HTTP/Celery/RAG/Qwen acceptance. Creates clearly marked synthetic records only."""

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx


class AcceptanceFailure(Exception):
    pass


async def run(args):
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "provenance": "synthetic_acceptance",
        "expert_validated": False,
        "steps": [],
        "resources": {},
    }
    token = os.environ.get(args.token_env, "")
    async with httpx.AsyncClient(timeout=30, trust_env=False) as transport:
        if args.development_login:
            from urllib.parse import urlsplit

            if urlsplit(args.api_url).hostname not in {"localhost", "127.0.0.1"}:
                raise AcceptanceFailure("development login is restricted to local API")
            response = await transport.post(
                args.idp_url.rstrip("/") + "/token",
                data={"username": "admin", "password": "123456", "grant_type": "password"},
            )
            if response.status_code != 200:
                raise AcceptanceFailure("development authentication failed")
            token = response.json()["access_token"]
        if not token:
            raise AcceptanceFailure(f"set {args.token_env} or use --development-login locally")

        async def call(method, path, payload=None):
            response = await transport.request(
                method,
                args.api_url.rstrip("/") + path,
                headers={"Authorization": "Bearer " + token},
                json=payload,
            )
            if not response.is_success:
                try:
                    code = response.json().get("code", "HTTP_ERROR")
                except ValueError:
                    code = "HTTP_ERROR"
                raise AcceptanceFailure(
                    f"{method} {path.split('?')[0]}: {response.status_code} {code}"
                )
            return response.json()

        async def wait(job_id):
            deadline = monotonic() + args.timeout
            while monotonic() < deadline:
                job = await call("GET", "/api/v1/jobs/" + job_id)
                if job["status"] == "succeeded":
                    if job["completed_at"] is None:
                        raise AcceptanceFailure("job completion timestamp missing")
                    return job
                if job["status"] in {"failed", "cancelled"}:
                    raise AcceptanceFailure(
                        f"job {job_id} ended {job['status']}: {job.get('error_code')}"
                    )
                await asyncio.sleep(2)
            raise AcceptanceFailure(f"job {job_id} did not complete before timeout")

        def step(name, **data):
            report["steps"].append({"name": name, "status": "passed", **data})
            print(name + ": passed", flush=True)

        try:
            await call("GET", "/health/ready")
            await call("GET", "/api/v1/auth/me")
            step("runtime_and_authentication")
            marker = "ACCEPT-" + uuid4().hex[:10]
            base = await call("POST", "/api/v1/knowledge/bases", {"name": "[合成验收] " + marker})
            base_id = base["id"]
            report["resources"]["base_id"] = base_id
            document = await call(
                "POST",
                "/api/v1/knowledge/documents",
                {
                    "base_id": base_id,
                    "code": marker,
                    "title": "[合成验收] 阀门渗漏记录规范 " + marker,
                    "kind": "manual",
                },
            )
            text = (
                f"# 合成验收资料 {marker}\n本资料仅用于软件验收，不能用于现场操作。\n"
                "阀门出现轻微渗漏时，记录设备位号、观察时间、压力读数和现场照片，通知维检人员复核。"
                "不能仅凭照片推断泄漏原因。现场处置须经有资质人员审核。\n"
            )
            content = text.encode()
            created = await call(
                "POST",
                f"/api/v1/knowledge/documents/{document['id']}/versions",
                {
                    "filename": "acceptance.md",
                    "mime_type": "text/markdown",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "effective_from": "2026-01-01T00:00:00Z",
                },
            )
            version_id = created["version"]["id"]
            report["resources"]["version_id"] = version_id
            grant = created["upload"]
            uploaded = await transport.request(
                grant.get("method", "PUT"),
                grant["upload_url"],
                headers=grant.get("headers", {}),
                content=content,
            )
            if not uploaded.is_success:
                raise AcceptanceFailure("signed object upload failed")
            await call("POST", f"/api/v1/knowledge/versions/{version_id}/uploads:complete")
            processed = await call("POST", f"/api/v1/knowledge/versions/{version_id}:process")
            await wait(processed["job_id"])
            await call(
                "POST",
                f"/api/v1/knowledge/versions/{version_id}:review",
                {"approved": True, "reason": "仅确认合成测试资料入库，用于软件验收"},
            )
            await call("POST", f"/api/v1/knowledge/versions/{version_id}:publish")
            step("asynchronous_knowledge_ingestion")
            run = await call(
                "POST",
                "/api/v1/knowledge/runs",
                {"question": "阀门出现轻微渗漏时应记录什么信息？", "base_ids": [base_id]},
            )
            await wait(run["job_id"])
            answer = await call("GET", "/api/v1/knowledge/runs/" + run["id"])
            if answer["answer"].get("refused") or not answer["answer"].get("claims"):
                raise AcceptanceFailure("grounded synthetic answer was refused or empty")
            calls = await call("GET", f"/api/v1/jobs/{run['job_id']}/ai-calls")
            if not any(c["provider"] == "qwen" and c["status"] == "succeeded" for c in calls):
                raise AcceptanceFailure("real Qwen call not present in answer audit")
            step("real_grounded_qwen_answer", run_id=run["id"])
            extracted = await call(
                "POST",
                "/api/v1/event-extractions",
                {
                    "source_type": "raw_text",
                    "raw_text": (
                        f"[合成验收 {marker}] 模拟二号阀门轻微渗漏，"
                        "已记录压力读数并通知维检，等待专业复核。"
                    ),
                },
            )
            if extracted.get("event_ids"):
                raise AcceptanceFailure("expected asynchronous extraction; inline mode was used")
            await wait(extracted["job_id"])
            results = await call("GET", f"/api/v1/event-extractions/{extracted['job_id']}/results")
            if len(results["event_ids"]) != 1:
                raise AcceptanceFailure("synthetic extraction must return one event")
            event_id = results["event_ids"][0]
            report["resources"]["event_id"] = event_id
            event = await call("GET", "/api/v1/events/" + event_id)
            await call(
                "POST",
                f"/api/v1/events/{event_id}:confirm",
                {"expected_version_id": event["current_version_id"]},
            )
            classified = await call("POST", f"/api/v1/events/{event_id}:classify")
            await wait(classified["job_id"])
            run_id = classified["agent_run_id"]
            report["resources"]["agent_run_id"] = run_id
            agent = await call("GET", "/api/v1/agent-runs/" + run_id)
            trace = await call("GET", f"/api/v1/agent-runs/{run_id}/trace")
            if not any(s["tool_name"] == "search_knowledge" for s in trace["steps"]):
                raise AcceptanceFailure("agent did not call the knowledge tool")
            # Workflow is the last classification success event's explicit result.
            history = await call("GET", f"/api/v1/jobs/{classified['job_id']}/event-history")
            workflows = [e["payload"].get("result", {}).get("workflow_id") for e in history]
            workflow_id = next((value for value in reversed(workflows) if value), None)
            if workflow_id is None:
                raise AcceptanceFailure("classification result did not identify workflow")
            workflow = await call("GET", "/api/v1/workflows/" + workflow_id)
            approved = await call(
                "POST",
                f"/api/v1/workflows/{workflow_id}:review",
                {
                    "approved": True,
                    "reason": "仅验收合成事件的工单草稿生成，不授权任何现场操作",
                    "expected_version": workflow["version"],
                },
            )
            report["resources"]["work_order_id"] = approved["work_order_id"]
            step("asynchronous_agent_rag_review_work_order", usage=agent["usage"])
            # Keep the resulting order in draft; no dispatch or field actions.
            order = await call("GET", "/api/v1/work-orders/" + approved["work_order_id"])
            if order["status"] != "draft":
                raise AcceptanceFailure("synthetic work order must stay draft")
            report["status"] = "passed"
        except (AcceptanceFailure, httpx.HTTPError) as error:
            report["status"] = "failed"
            report["failure"] = (
                str(error) if isinstance(error, AcceptanceFailure) else type(error).__name__
            )
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if report.get("status") != "passed":
        raise AcceptanceFailure(report.get("failure", "acceptance incomplete"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--idp-url", default="http://127.0.0.1:9001")
    parser.add_argument("--token-env", default="PIPECHINA_ACCESS_TOKEN")
    parser.add_argument("--development-login", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (AcceptanceFailure, httpx.HTTPError) as error:
        raise SystemExit(
            str(error) if isinstance(error, AcceptanceFailure) else type(error).__name__
        ) from None


if __name__ == "__main__":
    main()

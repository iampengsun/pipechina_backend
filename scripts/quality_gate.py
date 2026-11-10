"""Check approved expert targets against immutable, independently closed improvement records."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.modules.quality.domain.schemas import MetricTarget
from scripts.release_manifest import source_digest


class GatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expert_confirmed: bool
    expert_user_ids: list[UUID] = Field(min_length=1)
    approval_reference: str = Field(min_length=5)
    improvement_ids: dict[str, UUID] = Field(min_length=2, max_length=2)
    targets: dict[str, dict[str, MetricTarget]]


def inspect_record(kind, record, plan):
    if record["kind"] != kind or record["status"] != "closed":
        raise ValueError("both improvement records must be independently closed")
    review = next((h for h in reversed(record["history"]) if h["action"] == "accepted"), None)
    if review is None or review.get("actor_id") != record.get("reviewer_id"):
        raise ValueError("missing acceptance history")
    if record.get("reviewer_id") not in {str(key) for key in plan.expert_user_ids}:
        raise ValueError("acceptance reviewer is not in the confirmed expert list")
    retest = next((h for h in reversed(record["history"]) if h["action"] == "retested"), None)
    if retest is None or not retest["comparison"]["passed"]:
        raise ValueError("missing passing before/after comparison")
    if retest["retest"]["dataset"]["split"] != "held_out":
        raise ValueError("release requires the held-out split")
    configured = plan.targets[kind]
    if kind == "rag":
        modes = retest["retest"]["dataset"].get("modes", ["hybrid"])
        if len(modes) > 1:
            required = {
                f"{mode}.{name}"
                for mode in modes
                for name in ["answer_accuracy", "unsafe_answer_rate", "recall", "ndcg"]
            }
        else:
            required = {"answer_accuracy", "unsafe_answer_rate", "recall", "ndcg"}
    else:
        required = {
            "category_accuracy",
            "risk_accuracy",
            "risk_underestimation_rate",
            "unsafe_action_rate",
        }
    if not required <= configured.keys():
        raise ValueError("missing required safety and correctness targets per evaluation mode")
    for metric, target in configured.items():
        if metric in required:
            adverse = metric.rsplit(".", 1)[-1] in {
                "unsafe_answer_rate",
                "unsafe_action_rate",
                "risk_underestimation_rate",
            }
            if not 0 <= target.value <= 1 or target.operator != ("lte" if adverse else "gte"):
                raise ValueError("invalid safety target direction or probability range")
        stored = record["targets"].get(metric)
        if stored != target.model_dump():
            raise ValueError("release targets differ from independently accepted targets")
    return {"improvement_id": record["id"], "comparison": retest["comparison"]}


async def run(args):
    report: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "status": "blocked",
        "checks": {},
    }
    try:
        plan = GatePlan.model_validate_json(args.plan.read_text())
        if not plan.expert_confirmed or set(plan.improvement_ids) != {"rag", "agent"}:
            raise ValueError("expert confirmation and both evaluation kinds are required")
        acceptance = json.loads(args.real_acceptance.read_text())
        if acceptance.get("status") != "passed" or not any(
            step["name"] == "real_grounded_qwen_answer" and step["status"] == "passed"
            for step in acceptance.get("steps", [])
        ):
            raise ValueError("a passing real Qwen acceptance report is required")
        async with httpx.AsyncClient(
            base_url=args.api_url,
            timeout=30,
            trust_env=False,
            headers={"Authorization": "Bearer " + os.environ.get("PIPECHINA_ACCESS_TOKEN", "")},
        ) as client:
            for kind, key in plan.improvement_ids.items():
                response = await client.get("/api/v1/evaluation-improvements/" + str(key))
                if not response.is_success:
                    raise ValueError("improvement record is unavailable to current user")
                report["checks"][kind] = inspect_record(kind, response.json(), plan)
        report["status"] = "passed"
        report["source_digest"] = source_digest()
        report["approval_reference"] = plan.approval_reference
        report["meaning"] = (
            "confirmed targets and independent acceptance records verified; "
            "no inferred expert qualifications"
        )
    except (ValueError, KeyError, OSError, httpx.HTTPError) as exc:
        report["reason"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(report["status"])
    return report["status"] == "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--real-acceptance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()

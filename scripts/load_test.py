"""Bounded authenticated read load; writes and field actions are never sent."""

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
from statistics import median
from time import monotonic
from urllib.parse import urlsplit

import httpx


async def run(args):
    headers = {"Authorization": "Bearer " + os.environ.get("PIPECHINA_ACCESS_TOKEN", "")}
    durations = []
    statuses: dict[str, int] = {}
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        if args.development_login:
            if any(
                urlsplit(url).hostname not in {"localhost", "127.0.0.1"}
                for url in [args.api_url, args.idp_url]
            ):
                raise ValueError("development login requires localhost")
            r = await client.post(
                args.idp_url + "/token",
                data={"username": "admin", "password": "123456", "grant_type": "password"},
            )
            r.raise_for_status()
            headers = {"Authorization": "Bearer " + r.json()["access_token"]}

        async def request():
            async with semaphore:
                start = monotonic()
                try:
                    r = await client.get(args.api_url + "/api/v1/auth/me", headers=headers)
                    code = str(r.status_code)
                except httpx.HTTPError:
                    code = "transport_error"
                durations.append(monotonic() - start)
                statuses[code] = statuses.get(code, 0) + 1

        started = monotonic()
        await asyncio.gather(*(request() for _ in range(args.requests)))
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    report = {
        "at": datetime.now(UTC).isoformat(),
        "scope": "authenticated_read_only",
        "requests": args.requests,
        "concurrency": args.concurrency,
        "status_counts": statuses,
        "elapsed_seconds": monotonic() - started,
        "median_seconds": median(durations),
        "p95_seconds": p95,
        "status": "passed"
        if statuses == {"200": args.requests} and p95 <= args.max_p95
        else "failed",
        "meaning": "observed envelope only; not a production capacity guarantee",
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(report["status"], "p95", round(p95, 3))
    return report["status"] == "passed"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-url", default="http://127.0.0.1:8000")
    p.add_argument("--idp-url", default="http://127.0.0.1:9001")
    p.add_argument("--development-login", action="store_true")
    p.add_argument("--requests", type=int, default=120)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--max-p95", type=float, default=2)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if not 1 <= args.requests <= 500 or not 1 <= args.concurrency <= 50:
        raise SystemExit("requests must be 1..500, concurrency 1..50")
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()

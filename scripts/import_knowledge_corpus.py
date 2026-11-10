"""通过知识API和存储上传授权批量导入已生成的知识测试语料。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx


async def _json(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


async def import_corpus(
    root: Path, api_url: str, token: str, base_id: str | None, process: bool = False
) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    authorization = {"Authorization": f"Bearer {token}"}
    async with (
        httpx.AsyncClient(
            base_url=api_url.rstrip("/"), headers=authorization, timeout=120
        ) as client,
        httpx.AsyncClient(timeout=120) as upload_client,
    ):
        if base_id is None:
            base = await _json(
                client,
                "POST",
                "/knowledge/bases",
                json={"name": manifest["corpus_name"], "reader_ids": []},
            )
            base_id = base["id"]
        for document in manifest["documents"]:
            created = await _json(
                client,
                "POST",
                "/knowledge/documents",
                json={
                    "base_id": base_id,
                    "code": document["code"],
                    "title": document["title"],
                    "kind": document["kind"],
                },
            )
            for version in document["versions"]:
                payload = {
                    key: version[key]
                    for key in (
                        "filename",
                        "mime_type",
                        "size_bytes",
                        "sha256",
                        "effective_from",
                        "equipment_models",
                        "authority",
                    )
                }
                session = await _json(
                    client,
                    "POST",
                    f"/knowledge/documents/{created['id']}/versions",
                    json=payload,
                )
                upload = session["upload"]
                response = await upload_client.request(
                    upload.get("method", "PUT"),
                    upload["upload_url"],
                    content=(root / version["path"]).read_bytes(),
                    headers=upload.get("headers", {}),
                )
                response.raise_for_status()
                version_id = session["version"]["id"]
                await _json(client, "POST", f"/knowledge/versions/{version_id}/uploads:complete")
                if process:
                    await _json(client, "POST", f"/knowledge/versions/{version_id}:process")
                print(f"uploaded {document['code']} v{version['number']}: {version['filename']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("dev/fixtures/knowledge_corpus"))
    parser.add_argument("--api-url", default="http://localhost:8000/api/v1")
    parser.add_argument("--token", required=True)
    parser.add_argument("--base-id")
    parser.add_argument("--process", action="store_true")
    args = parser.parse_args()
    asyncio.run(import_corpus(args.root, args.api_url, args.token, args.base_id, args.process))

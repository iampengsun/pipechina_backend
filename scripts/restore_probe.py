"""Read-only aggregate recovery checks inside a service's isolated network namespace."""

import hashlib
import json
import sys

import httpx


def probe(kind, environment):
    env = dict(item.split("=", 1) for item in environment)
    with httpx.Client(timeout=15, trust_env=False) as client:
        if kind == "elasticsearch":
            response = client.get(
                "http://127.0.0.1:9200/_stats/docs", auth=("elastic", env["ELASTIC_PASSWORD"])
            )
            response.raise_for_status()
            return {
                name: value["primaries"]["docs"]["count"]
                for name, value in response.json()["indices"].items()
            }
        if kind == "qdrant":
            headers = {"api-key": env["QDRANT__SERVICE__API_KEY"]}
            response = client.get("http://127.0.0.1:6333/collections", headers=headers)
            response.raise_for_status()
            counts = {}
            for item in response.json()["result"]["collections"]:
                name = item["name"]
                response = client.post(
                    f"http://127.0.0.1:6333/collections/{name}/points/count",
                    headers=headers,
                    json={"exact": True},
                )
                response.raise_for_status()
                counts[name] = response.json()["result"]["count"]
            return counts
        if kind == "neo4j":
            username, password = env["NEO4J_AUTH"].split("/", 1)
            response = client.post(
                "http://127.0.0.1:7474/db/neo4j/tx/commit",
                auth=(username, password),
                json={
                    "statements": [
                        {
                            "statement": "MATCH (n) RETURN labels(n) AS labels, "
                            "count(n) AS count ORDER BY labels"
                        },
                        {
                            "statement": "MATCH ()-[r]->() RETURN type(r) AS type, "
                            "count(r) AS count ORDER BY type"
                        },
                    ]
                },
            )
            response.raise_for_status()
            body = response.json()
            if body["errors"]:
                raise ValueError("graph recovery query failed")
            return [[row["row"] for row in result["data"]] for result in body["results"]]
    if kind == "seaweedfs":
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url="http://127.0.0.1:8333",
            region_name="us-east-1",
            aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
            config=Config(s3={"addressing_style": "path"}, connect_timeout=10, read_timeout=15),
        )
        contents: list[tuple[str, str, int, str, str]] = []
        for bucket in client.list_buckets()["Buckets"]:
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket["Name"]):
                for obj in page.get("Contents", []):
                    response = client.get_object(Bucket=bucket["Name"], Key=obj["Key"])
                    sha = hashlib.sha256()
                    with response["Body"] as stream:
                        while block := stream.read(1024 * 1024):
                            sha.update(block)
                    contents.append(
                        (bucket["Name"], obj["Key"], obj["Size"], obj["ETag"], sha.hexdigest())
                    )
        return {
            "objects": len(contents),
            "digest": hashlib.sha256(json.dumps(sorted(contents)).encode()).hexdigest(),
        }
    raise ValueError("unsupported recovery service")


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(probe(request["service"], request["environment"]), sort_keys=True))

from __future__ import annotations

from app.bootstrap import ensure_bucket as bucket_module
from app.bootstrap.config import Settings


class FakeS3Client:
    def __init__(self, bucket_names: list[str] | None = None, fail_once: bool = False) -> None:
        self.bucket_names = bucket_names or []
        self.fail_once = fail_once
        self.list_calls = 0
        self.created: list[dict[str, object]] = []

    def list_buckets(self) -> dict[str, list[dict[str, str]]]:
        self.list_calls += 1
        if self.fail_once and self.list_calls == 1:
            raise OSError("object storage is still starting")
        return {"Buckets": [{"Name": name} for name in self.bucket_names]}

    def create_bucket(self, **kwargs: object) -> None:
        self.created.append(kwargs)


def storage_settings(region: str = "us-east-1") -> Settings:
    return Settings(
        storage_provider="s3",
        s3_endpoint_url="http://seaweedfs:8333",
        s3_access_key="pipechina",
        s3_secret_key="replace-me",
        s3_bucket="pipechina-private",
        s3_region=region,
        s3_addressing_style="path",
    )


def test_bucket_bootstrap_is_idempotent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = FakeS3Client(bucket_names=["pipechina-private"])
    monkeypatch.setattr(bucket_module, "get_settings", storage_settings)
    monkeypatch.setattr(bucket_module.boto3, "client", lambda *args, **kwargs: client)

    bucket_module.ensure_bucket()

    assert client.list_calls == 1
    assert client.created == []


def test_bucket_bootstrap_retries_and_creates_regional_bucket(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = FakeS3Client(fail_once=True)
    monkeypatch.setattr(bucket_module, "get_settings", lambda: storage_settings("cn-north-1"))
    monkeypatch.setattr(bucket_module.boto3, "client", lambda *args, **kwargs: client)
    monkeypatch.setattr(bucket_module.time, "sleep", lambda _: None)

    bucket_module.ensure_bucket()

    assert client.list_calls == 2
    assert client.created == [
        {
            "Bucket": "pipechina-private",
            "CreateBucketConfiguration": {"LocationConstraint": "cn-north-1"},
        }
    ]

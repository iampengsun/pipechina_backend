"""为本地容器环境创建配置中的S3存储桶，重复执行不会产生副作用。"""

from __future__ import annotations

import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.bootstrap.config import get_settings


def ensure_bucket() -> None:
    """连接S3并确保目标私有存储桶存在。"""

    settings = get_settings()
    secret_key = settings.s3_secret_key.get_secret_value() if settings.s3_secret_key else ""
    credentials: dict[str, str] = {}
    if bool(settings.s3_access_key) != bool(secret_key):
        raise ValueError("S3_ACCESS_KEY and S3_SECRET_KEY must be configured together")
    if settings.s3_access_key and secret_key:
        credentials = {
            "aws_access_key_id": settings.s3_access_key,
            "aws_secret_access_key": secret_key,
        }
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.s3_addressing_style},
        ),
        **credentials,
    )
    for attempt in range(30):
        try:
            buckets = {item["Name"] for item in client.list_buckets().get("Buckets", [])}
            if settings.s3_bucket not in buckets:
                kwargs: dict[str, object] = {"Bucket": settings.s3_bucket}
                if settings.s3_region != "us-east-1":
                    kwargs["CreateBucketConfiguration"] = {"LocationConstraint": settings.s3_region}
                client.create_bucket(**kwargs)
            return
        except BotoCoreError, ClientError, OSError:
            if attempt == 29:
                raise
            time.sleep(1)


if __name__ == "__main__":
    ensure_bucket()

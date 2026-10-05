"""모델·내보내기 파일 저장소(analytics-service.md §2: S3 호환 오브젝트 스토리지 + 메타데이터는 DB).

운영은 기존 S3 호환 저장소(storage.java21.net, Secret data2flow-archive)를 쓰고, 설정이 없으면 로컬 디렉터리에 둔다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...

    def get(self, uri: str) -> bytes: ...


class FileStore:
    def __init__(self, root: str):
        self.root = Path(root)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"file://{path}"

    def get(self, uri: str) -> bytes:
        return Path(uri.removeprefix("file://")).read_bytes()


class S3Store:
    def __init__(self, endpoint: str | None, access_key: str, secret_key: str, bucket: str, prefix: str = "analytics/", client=None):
        if client is None:
            import boto3

            client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                                  region_name="us-east-1")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        full = self.prefix + key
        self.client.put_object(Bucket=self.bucket, Key=full, Body=data, ContentType=content_type)
        return f"s3://{self.bucket}/{full}"

    def get(self, uri: str) -> bytes:
        rest = uri.removeprefix("s3://")
        bucket, key = rest.split("/", 1)
        return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()


def build_store(settings) -> ObjectStore:
    if settings.store_endpoint and settings.store_access_key and settings.store_secret_key and settings.store_bucket:
        return S3Store(settings.store_endpoint, settings.store_access_key, settings.store_secret_key, settings.store_bucket)
    return FileStore(settings.store_dir)

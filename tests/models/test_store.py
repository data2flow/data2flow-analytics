"""모델·내보내기 파일 저장소: S3 호환(boto3, moto 가짜 S3)과 파일."""

from __future__ import annotations

import boto3
from moto import mock_aws

from data2flow_analytics.config import Settings
from data2flow_analytics.models.store import FileStore, S3Store, build_store


def test_s3_store_roundtrip():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="data2flow-stg")
        store = S3Store(None, "k", "s", "data2flow-stg", client=client)
        uri = store.put("models/1/2/v1.pkl", b"model-bytes")
        assert uri == "s3://data2flow-stg/analytics/models/1/2/v1.pkl"
        assert store.get(uri) == b"model-bytes"


def test_file_store_and_build(tmp_path):
    store = FileStore(str(tmp_path))
    uri = store.put("exports/1/a.csv", b"x")
    assert store.get(uri) == b"x"
    assert isinstance(build_store(Settings(store_dir=str(tmp_path))), FileStore)
    with mock_aws():
        s3 = build_store(Settings(store_endpoint="https://storage.example", store_access_key="a", store_secret_key="b",
                                  store_bucket="data2flow-prod"))
        assert isinstance(s3, S3Store) and s3.bucket == "data2flow-prod"

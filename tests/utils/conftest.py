"""A configured bucket with a fake S3 client, for every test in this directory.

Autouse for the same reason `tests/homelab_facts/conftest.py` is: a test that
forgot it would build a real client from whatever the developer's environment
holds and either reach a real bucket or fail on a missing one. The fake stands in
for boto3's *effects* rather than its call shapes - what these tools promise is
that an object exists afterwards holding the bytes that were fetched, so the
tests assert on stored content and not only on a call log.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

ENDPOINT = "http://garage.test"
REGION = "homelab"
BUCKET = "datahub-local-temp"
PREFIX = "visual-studio/"


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.types: dict[str, str] = {}
        self.uploads = 0
        self.heads = 0
        self.signed: list[tuple[str, int]] = []

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        self.objects[key] = fileobj.read()
        if ExtraArgs and ExtraArgs.get("ContentType"):
            self.types[key] = ExtraArgs["ContentType"]
        self.uploads += 1

    def head_object(self, Bucket, Key):
        self.heads += 1
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key]), "ContentType": self.types.get(Key, "")}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.signed.append((Params["Key"], ExpiresIn))
        return (
            f"https://garage.test/{Params['Bucket']}/{Params['Key']}"
            f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=fake"
        )


@pytest.fixture(autouse=True)
def temp_bucket(monkeypatch):
    from utils import settings

    from mcp_runner.s3 import TempBucket

    client = FakeS3()
    bucket = TempBucket(
        endpoint=ENDPOINT, region=REGION, bucket=BUCKET, prefix=PREFIX, client=client
    )
    monkeypatch.setattr(settings, "bucket", lambda: bucket)
    return bucket


@pytest.fixture
def fake_s3(temp_bucket):
    """The fake client behind the configured bucket."""
    return temp_bucket._client


@pytest.fixture
def unconfigured(monkeypatch):
    """A bucket with nothing set, which is a supported state and not a crash."""
    from utils import settings

    from mcp_runner.s3 import TempBucket

    bucket = TempBucket(endpoint=None, region=None, bucket=None, prefix=None, client=FakeS3())
    monkeypatch.setattr(settings, "bucket", lambda: bucket)
    return bucket


def pairs(line: str) -> dict[str, str]:
    """Parse an answer the way a caller does: whitespace, then the first `=`."""
    return dict(part.split("=", 1) for part in line.split() if "=" in part)

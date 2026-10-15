"""The temp bucket's key policy and its bounds.

The policy is the reason a nested key cannot be written at all. The deployment
prunes the bucket by listing its prefix with a `/` delimiter, so a key with a
further slash is returned as a CommonPrefix rather than an object: the prune never
sees it, and the object stays in the bucket for ever. Enforcing it in the client
rather than trusting the caller is what makes that unreachable.
"""

from __future__ import annotations

import io

import pytest
from botocore.exceptions import ClientError

from mcp_runner.s3 import (
    MAX_KEY_BYTES,
    MAX_PRESIGN_SECONDS,
    KeyRefused,
    S3Error,
    S3Unconfigured,
    TempBucket,
)

PREFIX = "visual-studio/"


def bucket(**overrides):
    values = {
        "endpoint": "http://garage.test",
        "region": "homelab",
        "bucket": "datahub-local-temp",
        "prefix": PREFIX,
        "client": object(),
    }
    values.update(overrides)
    return TempBucket(**values)


def test_a_flat_key_below_the_prefix_is_accepted():
    assert bucket().key_for("visual-studio/13356-animation.mp4") == "visual-studio/13356-animation.mp4"


def test_a_key_outside_the_prefix_is_refused():
    with pytest.raises(KeyRefused, match="prefix"):
        bucket().key_for("other/13356-animation.mp4")


def test_a_nested_key_is_refused_and_the_reason_is_the_prune():
    with pytest.raises(KeyRefused, match="flat below"):
        bucket().key_for("visual-studio/2026/13356.mp4")


def test_the_prefix_itself_is_not_an_object():
    with pytest.raises(KeyRefused):
        bucket().key_for(PREFIX)


@pytest.mark.parametrize(
    "key",
    ["visual-studio/a b.mp4", "visual-studio/a\tb.mp4", "visual-studio/../x.mp4", ""],
)
def test_whitespace_and_parent_paths_are_refused(key):
    with pytest.raises(KeyRefused):
        bucket().key_for(key)


def test_an_oversized_key_is_refused():
    with pytest.raises(KeyRefused, match="longer than"):
        bucket().key_for(PREFIX + "x" * MAX_KEY_BYTES)


def test_every_unset_destination_value_is_named_at_once():
    # Reported together: a caller given one name, then another, has spent two
    # runs to learn one thing.
    with pytest.raises(S3Unconfigured) as caught:
        bucket(endpoint=None, region=None, bucket=None, prefix=None).key_for("a/b.mp4")
    message = str(caught.value)
    for name in ("TEMP_S3_ENDPOINT", "TEMP_S3_REGION", "TEMP_BUCKET", "TEMP_PREFIX"):
        assert name in message


def test_an_unconfigured_bucket_is_constructible():
    # A cached accessor builds this before any tool knows whether the deployment
    # supplies a destination, so construction must never raise.
    assert not bucket(endpoint=None, prefix=None).configured()


class _Head:
    def __init__(self, fail=False):
        self.fail = fail

    def head_object(self, Bucket, Key):
        if self.fail:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {"ContentLength": 7, "ContentType": "video/mp4"}


def test_a_head_reports_the_size_and_type():
    assert bucket(client=_Head()).head("visual-studio/a.mp4") == {
        "bytes": 7,
        "content_type": "video/mp4",
    }


def test_a_head_of_a_missing_object_names_the_code_not_the_class():
    # `ClientError` says nothing; `NoSuchKey` says the object is not there.
    with pytest.raises(S3Error, match="NoSuchKey"):
        bucket(client=_Head(fail=True)).head("visual-studio/a.mp4")


class _Signer:
    def __init__(self):
        self.seen = []

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.seen.append((operation, Params["Key"], ExpiresIn))
        return f"https://garage.test/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


def test_a_presigned_url_is_clamped_to_seven_days():
    signer = _Signer()
    bucket(client=signer).presign_get("visual-studio/a.mp4", 10**9)
    assert signer.seen[-1][2] == MAX_PRESIGN_SECONDS


def test_a_presigned_url_keeps_a_shorter_lifetime():
    signer = _Signer()
    bucket(client=signer).presign_get("visual-studio/a.mp4", 60)
    assert signer.seen[-1][2] == 60


class _Uploader:
    def __init__(self, fail=False):
        self.fail = fail
        self.seen = []

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        if self.fail:
            raise RuntimeError("nope")
        self.seen.append((key, fileobj.read(), ExtraArgs))


def test_a_streamed_put_reports_the_size_it_wrote():
    uploader = _Uploader()
    size = bucket(client=uploader).put_stream("visual-studio/a.mp4", io.BytesIO(b"12345"))
    assert size == 5
    assert uploader.seen == [("visual-studio/a.mp4", b"12345", None)]


def test_a_put_carries_the_content_type_when_one_is_given():
    uploader = _Uploader()
    bucket(client=uploader).put_stream("visual-studio/a.mp4", io.BytesIO(b"x"), "video/mp4")
    assert uploader.seen[-1][2] == {"ContentType": "video/mp4"}


def test_a_failed_put_is_an_s3_error_rather_than_a_boto_one():
    # The tool catches this family and turns it into an answer; letting boto3's
    # own exception out would end the call instead.
    with pytest.raises(S3Error, match="RuntimeError"):
        bucket(client=_Uploader(fail=True)).put_stream("visual-studio/a.mp4", io.BytesIO(b"x"))

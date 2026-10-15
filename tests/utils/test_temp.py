"""The two tools, and the contract a caller parses.

Three things are under test rather than described. The key policy, because the
prune lists a prefix with a `/` delimiter and a nested key is therefore an object
nothing ever deletes. The answer shape, because the caller is a workflow that
splits on whitespace rather than a model reading prose. And the failure modes,
because every one of them is a way to report an asset that does not exist.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from utils.tools import temp

from .conftest import pairs

SOURCE = "http://session.test/artifacts/run_a/out.mp4"
KEY = "visual-studio/run_a-animation.mp4"


@respx.mock
def test_a_store_writes_the_object_and_answers_with_the_key_and_the_size(fake_s3):
    respx.get(SOURCE).mock(
        return_value=httpx.Response(
            200, content=b"rendered", headers={"content-type": "video/mp4"}
        )
    )
    answer = temp.temp_store(SOURCE, KEY)
    parsed = pairs(answer)
    assert parsed["key"] == KEY
    assert parsed["bytes"] == "8"
    assert parsed["content-type"] == "video/mp4"
    # The object exists holding exactly what was fetched, which is the promise.
    assert fake_s3.objects[KEY] == b"rendered"


@respx.mock
def test_the_caller_can_override_the_content_type(fake_s3):
    respx.get(SOURCE).mock(
        return_value=httpx.Response(200, content=b"x", headers={"content-type": "text/plain"})
    )
    assert pairs(temp.temp_store(SOURCE, KEY, content_type="video/mp4"))["content-type"] == "video/mp4"
    assert fake_s3.types[KEY] == "video/mp4"


@respx.mock
def test_a_source_that_is_not_200_stores_nothing(fake_s3):
    respx.get(SOURCE).mock(return_value=httpx.Response(404))
    answer = temp.temp_store(SOURCE, KEY)
    assert answer.startswith("ERROR:")
    assert "404" in answer
    assert fake_s3.uploads == 0


@respx.mock
def test_an_empty_source_is_refused_rather_than_stored(fake_s3):
    # A render that produced no frames answers 200 with nothing in it. Storing
    # that would hand the workflow a key to an empty asset.
    respx.get(SOURCE).mock(return_value=httpx.Response(200, content=b""))
    answer = temp.temp_store(SOURCE, KEY)
    assert answer.startswith("ERROR:")
    assert "no bytes" in answer
    assert fake_s3.uploads == 0


@respx.mock
def test_a_source_over_the_cap_is_refused_from_its_declared_length(fake_s3):
    # The declared length is checked before the body is read, so an oversized
    # source costs a header rather than 256 MB of transfer.
    respx.get(SOURCE).mock(
        return_value=httpx.Response(
            200, content=b"x", headers={"content-length": str(10**12)}
        )
    )
    answer = temp.temp_store(SOURCE, KEY)
    assert answer.startswith("ERROR:")
    assert "byte cap" in answer
    assert fake_s3.uploads == 0


@respx.mock
def test_a_fetch_that_fails_is_reported_as_a_fetch_failure(fake_s3):
    respx.get(SOURCE).mock(side_effect=httpx.ConnectError("no route"))
    answer = temp.temp_store(SOURCE, KEY)
    assert answer.startswith("ERROR:")
    assert "ConnectError" in answer
    assert fake_s3.uploads == 0


@pytest.mark.parametrize(
    "key, expected",
    [
        ("elsewhere/run_a.mp4", "not under this deployment's prefix"),
        ("visual-studio/nested/run_a.mp4", "flat below"),
        ("visual-studio/", "flat below"),
        ("visual-studio/run a.mp4", "whitespace"),
        ("visual-studio/../escape.mp4", "whitespace or a parent path"),
        ("", "no key was given"),
    ],
)
@respx.mock
def test_a_key_outside_the_policy_is_refused_before_anything_is_fetched(fake_s3, key, expected):
    # No route is registered: a refusal that still fetched would raise here.
    answer = temp.temp_store(SOURCE, key)
    assert answer.startswith("ERROR:")
    assert expected in answer
    assert fake_s3.uploads == 0


def test_an_unconfigured_bucket_is_an_error_and_not_a_silent_success(unconfigured):
    answer = temp.temp_store(SOURCE, KEY)
    assert answer.startswith("ERROR:")
    for name in ("TEMP_S3_ENDPOINT", "TEMP_S3_REGION", "TEMP_BUCKET", "TEMP_PREFIX"):
        assert name in answer
    assert "Nothing was stored" in answer


def test_a_retrieve_signs_a_url_and_reports_the_size(fake_s3):
    fake_s3.objects[KEY] = b"rendered"
    fake_s3.types[KEY] = "video/mp4"
    parsed = pairs(temp.temp_retrieve(KEY))
    assert parsed["key"] == KEY
    assert parsed["bytes"] == "8"
    assert parsed["content-type"] == "video/mp4"
    assert parsed["expires"] == "3600"
    assert parsed["url"].startswith("https://garage.test/")


def test_a_retrieve_honours_the_callers_lifetime(fake_s3):
    fake_s3.objects[KEY] = b"rendered"
    assert pairs(temp.temp_retrieve(KEY, expires_seconds=60))["expires"] == "60"
    assert fake_s3.signed[-1] == (KEY, 60)


def test_a_retrieve_beyond_seven_days_is_clamped_rather_than_signed(fake_s3):
    # S3 rejects a longer signature, so signing one would hand back a URL that
    # 403s with nothing in the answer to say why.
    fake_s3.objects[KEY] = b"rendered"
    assert pairs(temp.temp_retrieve(KEY, expires_seconds=10**9))["expires"] == "604800"


def test_a_missing_object_is_an_error_not_an_empty_one(fake_s3):
    answer = temp.temp_retrieve(KEY)
    assert answer.startswith("ERROR:")
    assert "404" in answer
    assert fake_s3.signed == []


def test_a_refused_key_is_not_signed_for(unconfigured):
    assert temp.temp_retrieve(KEY).startswith("ERROR:")


@respx.mock
def test_the_answer_is_one_line_of_pairs_a_caller_can_split(fake_s3):
    # The contract the workflow depends on: one line, whitespace-separated, and
    # the first `=` is the separator. A change to prose here breaks a parser.
    respx.get(SOURCE).mock(return_value=httpx.Response(200, content=b"rendered"))
    answer = temp.temp_store(SOURCE, KEY)
    assert "\n" not in answer
    assert pairs(answer)["key"] == KEY

"""`temp_store()` and `temp_retrieve()` - the temp bucket, for callers that must not carry bytes.

The pair exists because of one invariant: an execution n8n saves may carry a
rendered asset's coordinates and never its bytes. n8n persists binaries as well
as JSON - measured on this instance, 249 MB of `binary_data` across 903 rows in a
1335 MB database - so a workflow that fetches an asset and uploads it keeps a
copy of it on the way past. `temp_store` moves the file between two processes
that are not n8n: the producer serves it, this server fetches it, Garage keeps
it. `temp_retrieve` signs a URL, so a caller that needs the bytes back takes them
from Garage rather than through a tool answer.

Four things about the answers are deliberate.

- **The answer is one line of `key=value` pairs.** These tools are called by a
  workflow, not read by a model, so the shape is fixed and the caller parses it
  rather than matching prose: `key=<object> bytes=<size> content-type=<type>`
  from a store, `url=<signed> key=<object> bytes=<size> content-type=<type>
  expires=<seconds>` from a retrieve. Every failure is one line beginning
  `ERROR:` and naming what was not done, so a caller can retry in the next call
  instead of ending a run.
- **The size is load-bearing.** A caller that stored a file and cannot say how
  big it is has no way to notice it stored an empty one, and a render that
  produced no frames is a real failure mode rather than a theoretical one, so a
  zero-byte source is refused rather than stored.
- **A signed URL is a credential-bearing string.** S3 presigning puts the access
  key id and the signature in the query, so the URL is usable by whoever holds it
  until it expires. It is returned because that is what makes a download possible
  without a caller holding a key at all - do not paste one somewhere public.
- **The fetch is bounded in time and size**, streamed to a spooled file rather
  than held in memory, and refused unless the source answers `200`.

See ../../../README.md.
"""

from __future__ import annotations

import tempfile

import httpx

from mcp_runner.budget import truncate_lines
from mcp_runner.config import tool_budget
from mcp_runner.s3 import KeyRefused, S3Error, S3Unconfigured

from .. import settings

STORE_BUDGET = tool_budget("temp_store")
RETRIEVE_BUDGET = tool_budget("temp_retrieve")


def _pair_line(**pairs: object) -> str:
    """One `key=value` line, in the order given, with no value ever empty.

    A caller splitting on whitespace and then on the first `=` gets the same
    answer from every tool here. `content-type` carries the value the caller gave
    or the one the source declared, and says `application/octet-stream` when
    neither did, so a field is never missing rather than sometimes present.
    """
    return " ".join(f"{name}={value}" for name, value in pairs.items() if value not in (None, ""))


def temp_store(source_url: str, key: str, content_type: str | None = None) -> str:
    bucket = settings.bucket()
    try:
        target = bucket.key_for(key)
    except (KeyRefused, S3Unconfigured) as exc:
        return f"ERROR: {exc}. Nothing was stored."

    resolved_type = content_type or ""
    try:
        with httpx.stream(
            "GET", source_url, follow_redirects=True, timeout=settings.fetch_timeout()
        ) as response:
            if response.status_code != 200:
                return (
                    f"ERROR: {source_url} answered HTTP {response.status_code}. "
                    "Nothing was stored."
                )
            resolved_type = resolved_type or response.headers.get("content-type", "")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > settings.MAX_OBJECT_BYTES:
                return (
                    f"ERROR: {source_url} declares {declared} bytes, over this server's "
                    f"{settings.MAX_OBJECT_BYTES} byte cap. Nothing was stored."
                )
            with tempfile.SpooledTemporaryFile(max_size=settings.SPOOL_BYTES) as spool:
                written = 0
                for chunk in response.iter_bytes(settings.CHUNK_BYTES):
                    written += len(chunk)
                    if written > settings.MAX_OBJECT_BYTES:
                        return (
                            f"ERROR: {source_url} exceeded this server's "
                            f"{settings.MAX_OBJECT_BYTES} byte cap after {written} bytes. "
                            "Nothing was stored."
                        )
                    spool.write(chunk)
                if not written:
                    return (
                        f"ERROR: {source_url} answered 200 with no bytes. An empty asset is "
                        "a failed render, not a stored object. Nothing was stored."
                    )
                size = bucket.put_stream(target, spool, resolved_type or None)
    except httpx.HTTPError as exc:
        return f"ERROR: fetching {source_url} failed: {type(exc).__name__}. Nothing was stored."
    except S3Error as exc:
        return f"ERROR: {exc}. Nothing was stored."

    return truncate_lines(
        [
            _pair_line(
                key=target,
                bytes=size,
                **{"content-type": resolved_type or "application/octet-stream"},
            )
        ],
        STORE_BUDGET,
        unit="lines",
    )


def temp_retrieve(key: str, expires_seconds: int | None = None) -> str:
    bucket = settings.bucket()
    try:
        target = bucket.key_for(key)
        head = bucket.head(target)
        ttl = settings.presign_ttl() if expires_seconds is None else int(expires_seconds)
        url = bucket.presign_get(target, ttl)
    except (KeyRefused, S3Unconfigured, S3Error) as exc:
        return f"ERROR: {exc}. Nothing was signed."

    return truncate_lines(
        [
            _pair_line(
                url=url,
                key=target,
                bytes=head["bytes"],
                **{"content-type": head["content_type"] or "application/octet-stream"},
                expires=max(1, min(ttl, 604800)),
            )
        ],
        RETRIEVE_BUDGET,
        unit="lines",
    )


# -- tool manifest -----------------------------------------------------------

STORE_SCHEMA = {
    "type": "object",
    "properties": {
        "source_url": {
            "type": "string",
            "description": "HTTP(S) URL the bytes are fetched from. Fetched once, streaming.",
        },
        "key": {
            "type": "string",
            "description": (
                "Object key to store under. Must sit flat below this deployment's prefix, "
                "e.g. visual-studio/13356-animation.mp4."
            ),
        },
        "content_type": {
            "type": "string",
            "description": "Content type to store. Omit to use the source's own header.",
        },
    },
    "required": ["source_url", "key"],
    "additionalProperties": False,
}

RETRIEVE_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": "Object key to sign a download URL for.",
        },
        "expires_seconds": {
            "type": "integer",
            "description": "How long the URL stays valid. Defaults to this deployment's TTL.",
        },
    },
    "required": ["key"],
    "additionalProperties": False,
}

STORE_DESCRIPTION = """
Store a file in the temp bucket without carrying it: this server fetches
source_url and writes it to key, then answers with the key and the byte count.

Call this instead of downloading a file and uploading it yourself - a workflow
that does that keeps a copy of the bytes in its own execution record. The source
is fetched once, streaming, and refused unless it answers 200 with a non-zero
body. The answer is one line of key=value pairs; a failure is one line beginning
ERROR:.
"""

RETRIEVE_DESCRIPTION = """
Sign a short-lived URL that downloads one object from the temp bucket, and report
its size and content type.

Takes the object key and an optional lifetime in seconds. Nothing is copied and
nothing is read into the answer: the URL carries the object, so a caller that
needs the bytes fetches them from storage rather than through a tool result. The
URL is credential-bearing while it lasts - do not post it somewhere public. The
answer is one line of key=value pairs; a failure is one line beginning ERROR:.
"""

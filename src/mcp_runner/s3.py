"""The temp bucket over S3 - the one client here that writes and holds a credential.

Every other backend this package reaches is read-only and needs nothing secret:
Prometheus, Loki, the Kubernetes API and Trino are queried anonymously or through
the pod's ServiceAccount, and Garage's admin API takes an optional token whose
absence is a designed state. This client is different in both respects, and the
reason is one invariant: **an execution n8n saves may carry a rendered asset's
coordinates and never its bytes.**

n8n persists binaries as well as JSON - measured on the instance this was written
for, `binary_data` is 249 MB across 903 rows in a 1335 MB database - so a workflow
that fetches a rendered asset and uploads it keeps a copy of it on the way past,
and `saveDataSuccessExecution` has to stay off for that workflow. Moving the bytes
between two processes that are not n8n is what lets the workflow hold only the
key: the producer serves the file, this process fetches it, Garage keeps it.

Three consequences shape the code.

- **The key policy is enforced here, not trusted from the caller.** The deployment
  prunes the bucket by listing a prefix with a `/` delimiter, so a key with a
  further slash comes back as a CommonPrefix rather than an object and is never
  deleted: it would sit in the bucket forever. A key outside the configured
  prefix, or with a second slash, is refused.
- **The destination has no default.** A guessed endpoint or bucket writes to
  somewhere nobody prunes and reports a key no reader can resolve, so all four
  values come from the deployment's environment and their absence is a readable
  failure rather than a silent success. They are read as optional here, exactly as
  `garage_admin_url` is, because a cached accessor constructs this object before
  any tool knows whether the deployment supplies them - the unconfigured path is
  `S3Unconfigured`, which the caller renders as an error.
- **Credentials are the SDK's own environment** - `AWS_ACCESS_KEY_ID` and
  `AWS_SECRET_ACCESS_KEY`, or `AWS_SHARED_CREDENTIALS_FILE` - resolved by boto3's
  standard chain, so no value is read into code, a log line or a tool answer.

The key this server is given should be scoped to the one bucket it writes. `write`
alone is enough to store; `temp_retrieve` signs with the same key, so a deployment
that hands out download URLs grants `read` as well.
"""

from __future__ import annotations

import logging
import os
from typing import Any, BinaryIO

import boto3
from botocore.config import Config

from . import config

logger = logging.getLogger(__name__)

# An object name below the prefix: enough for a run id and an extension, short
# enough that a caller cannot paste a paragraph into the bucket listing.
MAX_KEY_BYTES = 512

# SigV4 presigned URLs are rejected beyond seven days whatever a caller asks for,
# so a longer request is clamped rather than signed into something that 403s.
MAX_PRESIGN_SECONDS = 7 * 24 * 3600

# What the deployment names the four destination values. Listed once so a missing
# one is reported by name.
_DESTINATION_VARS = ("TEMP_S3_ENDPOINT", "TEMP_S3_REGION", "TEMP_BUCKET", "TEMP_PREFIX")


class S3Error(RuntimeError):
    """The bucket could not be reached. Never a silent success."""


class S3Unconfigured(S3Error):
    """A destination value is unset. A supported state, and distinct from a failure."""


class KeyRefused(S3Error):
    """The key is outside this deployment's prefix, or is not flat below it."""


class TempBucket:
    def __init__(
        self,
        endpoint: str | None = None,
        region: str | None = None,
        bucket: str | None = None,
        prefix: str | None = None,
        client: Any = None,
    ):
        self.endpoint = endpoint if endpoint is not None else config.temp_s3_endpoint()
        self.region = region if region is not None else config.temp_s3_region()
        self.bucket = bucket if bucket is not None else config.temp_bucket()
        self.prefix = prefix if prefix is not None else config.temp_prefix()
        self._client = client

    def configured(self) -> bool:
        return bool(self.endpoint and self.region and self.bucket and self.prefix)

    def destination(self) -> None:
        """Refuse every call while a destination value is unset.

        All four together rather than one at a time: a tool that reports
        `TEMP_BUCKET` unset, is given one, and then fails on `TEMP_PREFIX` has
        cost its caller two runs to say one thing.
        """
        missing = [name for name, value in zip(_DESTINATION_VARS, self._values()) if not value]
        if missing:
            raise S3Unconfigured(
                f"{' and '.join(missing)} unset, so the temp bucket cannot be used. There is "
                "deliberately no default: a guessed endpoint or bucket writes to somewhere "
                "nobody prunes and reports a key no reader can resolve."
            )

    def _values(self) -> tuple[str | None, ...]:
        return (self.endpoint, self.region, self.bucket, self.prefix)

    def client(self) -> Any:
        self.destination()
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                region_name=self.region,
                # Garage serves S3 path-style on one endpoint rather than as
                # subdomains of it, and the wrong addressing style is a DNS
                # failure that reads as an unreachable bucket.
                config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
            )
        return self._client

    def key_for(self, key: str) -> str:
        """The object key, or a refusal naming the rule it breaks."""
        self.destination()
        candidate = (key or "").strip()
        if not candidate:
            raise KeyRefused("no key was given")
        if len(candidate.encode()) > MAX_KEY_BYTES:
            raise KeyRefused(f"the key is longer than {MAX_KEY_BYTES} bytes")
        if any(character.isspace() for character in candidate) or ".." in candidate:
            raise KeyRefused("the key contains whitespace or a parent path")
        if not candidate.startswith(self.prefix):
            raise KeyRefused(f"the key is not under this deployment's prefix {self.prefix!r}")
        name = candidate[len(self.prefix) :]
        if not name or "/" in name:
            raise KeyRefused(
                f"the object name must be flat below {self.prefix!r}: the bucket is pruned by "
                "listing that prefix with a '/' delimiter, so a nested key is never listed as "
                "an object and is never deleted"
            )
        return candidate

    def put_stream(self, key: str, stream: BinaryIO, content_type: str | None = None) -> int:
        """Write one object and return its size in bytes.

        `upload_fileobj` rather than `put_object`: a rendered video is tens of
        megabytes and this process should not hold one in memory. s3transfer
        streams it from the file object in parts.
        """
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        try:
            if content_type:
                self.client().upload_fileobj(
                    stream, self.bucket, key, ExtraArgs={"ContentType": content_type}
                )
            else:
                self.client().upload_fileobj(stream, self.bucket, key)
        except Exception as exc:  # boto3 raises a family; the name is the useful part
            raise S3Error(f"writing {key} failed: {type(exc).__name__}") from exc
        return size

    def head(self, key: str) -> dict[str, Any]:
        """One object's size and content type. A missing object is an error, not a zero."""
        try:
            response = self.client().head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise S3Error(f"reading {key} failed: {_error_code(exc)}") from exc
        return {
            "bytes": int(response.get("ContentLength") or 0),
            "content_type": str(response.get("ContentType") or ""),
        }

    def presign_get(self, key: str, expires: int = 3600) -> str:
        """A URL that downloads one object, signed with this deployment's key."""
        seconds = max(1, min(int(expires), MAX_PRESIGN_SECONDS))
        try:
            return self.client().generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=seconds,
            )
        except Exception as exc:
            raise S3Error(f"signing a URL for {key} failed: {type(exc).__name__}") from exc


def _error_code(exc: Exception) -> str:
    """An S3 error's own code, which says `404` where the class says `ClientError`."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__

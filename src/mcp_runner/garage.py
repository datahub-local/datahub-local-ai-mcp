"""Garage's admin API - two GETs, and the only credential this server can hold.

Everything else here reads Prometheus, Loki or the Kubernetes API, none of which
needs a secret. Per-bucket usage is the one fact with no unauthenticated source:
Garage publishes no bucket label and no stored-bytes gauge to Prometheus, so
bucket size and object count exist only behind a bearer token.

The token is therefore **optional and absent by default**. With no token the
caller reports the bucket section unavailable and every other reading is
untouched, so the server's "holds no credential" property is what you get unless
someone deliberately supplies one.

When one is supplied, the boundary is in the code rather than in the credential,
exactly as `kube.py` strips a Secret's payload however a caller asks for it:

- Two operations, both `GET`, both named here. There is no generic request
  method, so no caller can reach `CreateBucket` or `DeleteKey` even holding a
  token that would permit them.
- `keys` is dropped from every bucket. It carries access key ids, and this
  server does not return credentials any more than it returns a Secret's data.
- Garage v2 supports scoped admin tokens, so the token supplied should be
  scoped to `ListBuckets,GetBucketInfo` and the two rules above are then a
  second line rather than the only one.
"""

from __future__ import annotations

import logging

import httpx

from . import config

logger = logging.getLogger(__name__)

# Bucket ids and aliases only; the key grants a bucket carries are never read.
_REDACTED_FIELDS = frozenset({"keys"})

# A homelab has tens of buckets, not thousands, and each one costs a request.
MAX_BUCKETS = 50


class GarageError(RuntimeError):
    """The admin API could not be read. Never an empty bucket list."""


class GarageUnconfigured(GarageError):
    """No token was supplied. A supported state, and distinct from a failure."""


class Garage:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float | None = None):
        # Neither half raises here. A cached accessor constructs this before any
        # tool knows whether a token exists, so an unconfigured Garage must be a
        # constructible object whose *calls* fail - not a constructor that does.
        resolved = url if url is not None else config.garage_admin_url()
        self.url = resolved.rstrip("/") if resolved else None
        self.token = token if token is not None else config.garage_admin_token()
        self.timeout = timeout if timeout is not None else config.garage_timeout()

    def configured(self) -> bool:
        return bool(self.token and self.url)

    def _get(self, path: str, params: dict[str, str] | None = None) -> object:
        if not self.token:
            raise GarageUnconfigured("no GARAGE_ADMIN_TOKEN is set")
        if not self.url:
            raise GarageUnconfigured("no GARAGE_ADMIN_URL is set")
        try:
            response = httpx.get(
                f"{self.url}{path}",
                params=params or {},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The token itself never reaches the message: a 401 body can echo the
            # Authorization header back, and this string is rendered into a report.
            raise GarageError(f"{path} failed: {type(exc).__name__}") from exc

    def list_buckets(self) -> list[dict]:
        """Every bucket's id and aliases. `GET /v2/ListBuckets`."""
        payload = self._get("/v2/ListBuckets")
        if not isinstance(payload, list):
            raise GarageError("/v2/ListBuckets did not return a list")
        return [_strip(item) for item in payload if isinstance(item, dict)][:MAX_BUCKETS]

    def bucket_info(self, bucket_id: str) -> dict:
        """One bucket's usage: `bytes`, `objects`, `quotas`. `GET /v2/GetBucketInfo`."""
        payload = self._get("/v2/GetBucketInfo", {"id": bucket_id})
        if not isinstance(payload, dict):
            raise GarageError("/v2/GetBucketInfo did not return an object")
        return _strip(payload)


def _strip(obj: dict) -> dict:
    """Drop the key grants before they reach any caller.

    `GetBucketInfo` embeds each key's `accessKeyId` alongside its permissions.
    That is a credential identifier in a report that gets posted to Slack.
    """
    return {key: value for key, value in obj.items() if key not in _REDACTED_FIELDS}


def bucket_name(bucket: dict) -> str:
    """The name a person would recognise, falling back to the id.

    A bucket has global aliases, local aliases or neither; an id-only bucket is
    a real state rather than a missing name.
    """
    for field in ("globalAliases", "localAliases"):
        aliases = bucket.get(field)
        if isinstance(aliases, list) and aliases:
            return str(aliases[0])
    return str(bucket.get("id", "unknown"))[:16]

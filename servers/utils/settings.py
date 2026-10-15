"""This server's one client handle and its own bounds.

`utils` exists for the temp bucket and nothing else, so what it reads from the
environment is the bucket's four destination values, plus the three bounds this
server sets for itself: how long a fetch may take, how large an object it will
accept, and how long a signed download URL lives. The first four are deployment
facts with no safe default - see `mcp_runner.s3` - and the last three are
operational preferences whose safe value is the same everywhere.

The bounds are here rather than in a caller because they are what keeps one
careless call from taking the process down: a fetch with no timeout and no size
cap is a fetch that can hold a pod open for ever on a body it will never finish.
"""

from __future__ import annotations

import functools

from mcp_runner import config as runner_config
from mcp_runner.s3 import TempBucket

# A rendered video is tens of megabytes and a deck is a few hundred kilobytes, so
# this is two orders of magnitude above anything this pipeline produces and still
# far below what an emptyDir-backed pod can hold.
MAX_OBJECT_BYTES = 256 * 1024 * 1024

# What is held in memory before a fetch spills to disk. Above it, `temp_store`
# streams to /tmp, which the chart mounts as an emptyDir.
SPOOL_BYTES = 8 * 1024 * 1024

# Read size. Large enough that a 60 MB object is a few hundred reads.
CHUNK_BYTES = 256 * 1024


@functools.lru_cache(maxsize=1)
def bucket() -> TempBucket:
    """The temp bucket client, built once per process.

    Cached because boto3's client carries a connection pool and resolves the
    credential chain on construction, and a tool called once per asset in one
    run should not rebuild either.
    """
    return TempBucket()


def fetch_timeout() -> float:
    return float(runner_config.env("TEMP_FETCH_TIMEOUT_SECONDS", "120"))


def presign_ttl() -> int:
    """How long a download URL is valid.

    An hour by default: long enough for a caller to hand the URL to a review step
    and fetch it, short enough that one pasted somewhere it should not be is
    stale before it matters.
    """
    return int(runner_config.env("TEMP_PRESIGN_TTL_SECONDS", "3600"))

"""The only module that talks to Loki.

Same contract as `prometheus.py`, for the same reason: everything a prompt had
to teach the model is a property of the code here. No datasource uid, no LogQL
written by a model, no epoch arithmetic - the three values the oracle's prompt
spent a paragraph on and still got wrong, since Prometheus's uid is the bare
word `prometheus` while Loki's is a hex string, and a 4B model reads the bare
word as the placeholder.

A selector is built from values a tool *resolved*, never from words a person
typed. `LokiError` is a failed query and is never rendered as "no logs": a
lookup that could not run must never read as a lookup that found nothing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from . import config

logger = logging.getLogger(__name__)


class LokiError(RuntimeError):
    """A query failed. Distinct from an empty result, which is a real answer."""


@dataclass(frozen=True)
class Entry:
    """One log line with the stream it came from."""

    timestamp_ns: int
    labels: dict[str, str]
    line: str


def quote(value: str) -> str:
    """Quote a label value for LogQL - Go string rules, so both escapes matter."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def stream_selector(namespace: str, pod: str | None = None, container: str | None = None) -> str:
    """`{namespace="...",pod="...",container="..."}` from resolved values.

    Never a wildcard and never a guessed label key: these three are the labels
    this Loki actually carries, read off `/loki/api/v1/labels`. A caller with no
    pod name gets a namespace-wide stream, which is a real answer to "what is
    this namespace logging" and not a fishing expression.
    """
    parts = [f"namespace={quote(namespace)}"]
    if pod:
        parts.append(f"pod={quote(pod)}")
    if container:
        parts.append(f"container={quote(container)}")
    return "{" + ",".join(parts) + "}"


def contains_filter(text: str) -> str:
    """`|= "text"` - a literal substring, deliberately not a regex.

    `|~` would make an unanchored regex the caller's problem, and a bad one is a
    query error rather than an empty answer. Any string is valid here, which is
    the rule that makes an argument safe.
    """
    return f"|= {quote(text)}"


# Owned here rather than written into a prompt, which is where it was dropped,
# re-typed without the `(?i)`, and pasted with the surrounding quotes intact.
ERROR_FILTER = '|~ "(?i)error|fatal|panic|exception|oom|traceback"'


class Loki:
    def __init__(self, url: str | None = None, timeout: float | None = None) -> None:
        self.url = (url or config.loki_url()).rstrip("/")
        self.timeout = timeout if timeout is not None else config.loki_timeout()

    def query_range(
        self,
        query: str,
        *,
        since_seconds: int = 21600,
        limit: int = 100,
    ) -> list[Entry]:
        """Newest ``limit`` entries matching ``query`` in the last window.

        `direction=backward` with a limit is the only shape that bounds the
        answer: a busy namespace emits more in a minute than any budget here can
        carry, and a head-first read would return the oldest lines in the window
        rather than the ones the question is about.
        """
        end = time.time()
        params = {
            "query": query,
            "start": f"{int((end - since_seconds) * 1e9)}",
            "end": f"{int(end * 1e9)}",
            "limit": str(limit),
            "direction": "backward",
        }
        try:
            response = httpx.get(
                f"{self.url}/loki/api/v1/query_range", params=params, timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LokiError(f"query_range failed: {exc}") from exc
        if payload.get("status") != "success":
            raise LokiError(f"query_range returned status={payload.get('status')!r}")

        entries: list[Entry] = []
        for stream in (payload.get("data") or {}).get("result") or []:
            labels = stream.get("stream") or {}
            for value in stream.get("values") or []:
                try:
                    entries.append(
                        Entry(timestamp_ns=int(value[0]), labels=labels, line=str(value[1]))
                    )
                except (IndexError, TypeError, ValueError):
                    continue
        # Loki orders within a stream, not across them, so the merge happens here.
        entries.sort(key=lambda entry: entry.timestamp_ns, reverse=True)
        return entries[:limit]

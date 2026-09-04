"""The only module that talks to Prometheus.

Everything a prompt used to have to teach the model is a property of the code
here: no datasource uid, no `endTime`, no query-type argument, and a counter is
never returned raw. `UNAVAILABLE` is the other half of the contract - a query
that errors or matches nothing yields the sentinel, never `0` and never an
estimate. See ../README.md.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from . import config

logger = logging.getLogger(__name__)

# What a report prints when a metric answered nothing. A value, not an error.
UNAVAILABLE = "unavailable"


class PrometheusError(RuntimeError):
    """A query failed. Distinct from an empty result, which is a real answer."""


@dataclass(frozen=True)
class Reading:
    """Samples keyed by a label, plus whether the query succeeded at all.

    ``ok=False`` with an empty ``values`` is "unknown"; ``ok=True`` with an empty
    ``values`` is "nothing matched", which is a real answer.
    """

    values: dict[str, float]
    ok: bool

    def covered(self) -> set[str]:
        return set(self.values)


class Prometheus:
    def __init__(self, url: str | None = None, timeout: float | None = None) -> None:
        self.url = (url or config.prometheus_url()).rstrip("/")
        self.timeout = timeout if timeout is not None else config.prometheus_timeout()

    # -- transport -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> dict:
        try:
            response = httpx.get(f"{self.url}{path}", params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PrometheusError(f"{path} failed: {exc}") from exc
        if payload.get("status") != "success":
            raise PrometheusError(f"{path} returned status={payload.get('status')!r}")
        return payload.get("data") or {}

    # -- queries -------------------------------------------------------------

    def instant(self, expr: str) -> list[dict]:
        """Evaluate ``expr`` at server-now, returning the raw sample list.

        No `time` parameter is sent, so Prometheus evaluates at its own clock -
        the only clock here known to be right.
        """
        data = self._get("/api/v1/query", {"query": expr})
        return data.get("result") or []

    def range_(self, expr: str, *, since_seconds: int = 21600, step_seconds: int = 300) -> list[dict]:
        """Evaluate ``expr`` over the last ``since_seconds``, ending now.

        Prometheus rejects relative times - `start=now-1h` is a parse error, and
        `now-1h` is a Grafana convenience this API does not share - so the window
        is resolved to unix timestamps here.
        """
        end = time.time()
        data = self._get(
            "/api/v1/query_range",
            {
                "query": expr,
                "start": f"{end - since_seconds:.3f}",
                "end": f"{end:.3f}",
                "step": str(step_seconds),
            },
        )
        return data.get("result") or []

    def metric_type(self, metric: str) -> str | None:
        """Metric type from the metadata API - never inferred from the suffix,
        which misreads this cluster in both directions."""
        data = self._get("/api/v1/metadata", {"metric": metric})
        entries = data.get(metric) if isinstance(data, dict) else None
        if not entries:
            return None
        return entries[0].get("type")

    # -- shaping -------------------------------------------------------------

    def scalar_by(self, expr: str, label: str) -> dict[str, float]:
        """Return ``{label_value: sample}`` for a query keyed by ``label``.

        A series missing the label is dropped rather than given a made-up key, so
        a broken join surfaces as absences instead of a plausible wrong row.
        """
        out: dict[str, float] = {}
        for series in self.instant(expr):
            key = (series.get("metric") or {}).get(label)
            if not key:
                continue
            try:
                out[key] = float(series["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return out

    def try_scalar_by(self, expr: str, label: str) -> dict[str, float]:
        """``scalar_by`` that turns a query failure into an empty answer."""
        return self.reading(expr, label).values

    def reading(self, expr: str, label: str) -> Reading:
        """``scalar_by`` plus whether the query itself succeeded.

        Two different facts that get different words in a report: a query that
        answered nothing for one node means that node has no such sensor; a query
        that failed means the value is unknown for every node.
        """
        try:
            return Reading(values=self.scalar_by(expr, label), ok=True)
        except PrometheusError as exc:
            logger.warning("query failed, reporting unavailable: %s (%s)", expr, exc)
            return Reading(values={}, ok=False)


# -- expression helpers ------------------------------------------------------
#
# Each of these encodes a reading that is easy to get wrong, so a caller cannot.


def used_percent(available: str, capacity: str, selector: str = "") -> str:
    """Percent **used** - `100 * (1 - available/capacity)`, never the bare ratio,
    which is the fraction *free* and inverts every finding."""
    return f"100 * (1 - {available}{selector} / {capacity}{selector})"


def increase_(metric: str, window: str = "1h") -> str:
    """`increase(metric[window])` - the whole call, so the wrapper cannot be
    dropped and a raw counter mislabelled as the increase."""
    return f"increase({metric}[{window}])"


def rate_percent(metric: str, window: str = "5m") -> str:
    """Percentage of ``window`` spent in the state ``metric`` accumulates."""
    return f"100 * rate({metric}[{window}])"


def by_nodename(inner: str, aggregator: str = "max") -> str:
    """Attach machine identity to a `node_*` expression.

    No `node_*` series carries a machine name - they are keyed by `instance`, and
    the only hostname anywhere is `nodename` on `node_uname_info`. This join is
    the only way to ask, so it cannot be dropped.
    """
    return f"{aggregator} by (nodename) ({inner} * on(instance) group_left(nodename) node_uname_info)"

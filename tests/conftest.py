"""Test fixtures. Nothing here talks to a cluster."""

from __future__ import annotations

import pytest


class FakePrometheus:
    """Answers canned results, and records every expression it was sent.

    The recorded expressions are half the point: several tests assert on the
    *query* rather than the answer, because the failures being guarded against
    were wrong queries that returned perfectly valid numbers.
    """

    def __init__(self, answers: dict[str, list[dict]] | None = None, fail: set[str] | None = None):
        self.answers = answers or {}
        self.fail = fail or set()
        self.seen: list[str] = []

    def _lookup(self, expr: str) -> list[dict]:
        self.seen.append(expr)
        if expr in self.fail:
            from mcp_runner.prometheus import PrometheusError

            raise PrometheusError("injected failure")
        return self.answers.get(expr, [])

    def instant(self, expr):
        return self._lookup(expr)

    def range_(self, expr, **kwargs):
        return self._lookup(expr)

    def scalar_by(self, expr, label):
        out = {}
        for series in self._lookup(expr):
            key = (series.get("metric") or {}).get(label)
            if key:
                out[key] = float(series["value"][1])
        return out

    def try_scalar_by(self, expr, label):
        try:
            return self.scalar_by(expr, label)
        except Exception:  # noqa: BLE001 - mirrors the real client's degrade path
            return {}

    def reading(self, expr, label):
        from mcp_runner.prometheus import Reading

        try:
            return Reading(values=self.scalar_by(expr, label), ok=True)
        except Exception:  # noqa: BLE001 - mirrors the real client's degrade path
            return Reading(values={}, ok=False)


def sample(value: float, **labels) -> dict:
    return {"metric": labels, "value": [0, str(value)]}


@pytest.fixture
def fake_prometheus():
    """The FakePrometheus class itself, so a test can build one with its answers."""
    return FakePrometheus


@pytest.fixture
def sample_series():
    """Build one Prometheus sample: ``sample_series(1.0, nodename="n1")``."""
    return sample


class NoKube:
    """A Kubernetes client that knows about no nodes."""

    def __init__(self, names: list[str] | None = None):
        self._names = names or []

    def node_names(self) -> list[str]:
        return self._names

    def list(self, *args, **kwargs):
        return []


@pytest.fixture
def kube_with():
    """Factory for a stub Kubernetes client with a given node list."""
    return NoKube


@pytest.fixture
def state_dir(tmp_path):
    from mcp_runner.state import Snapshots

    return Snapshots(str(tmp_path / "state"))

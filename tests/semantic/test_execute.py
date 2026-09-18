"""`query`: what travels with the numbers, and what reaches Trino.

Two properties carry the weight here. The exclusions must survive every reply
shape - including a result wide enough to truncate, where a single budget over
the whole text would drop the caveats and leave the bare numbers. And the values
must arrive as bound parameters: the compiler's `?` placeholders are only a
boundary if nothing interpolates them on the way out.
"""

from __future__ import annotations

from datetime import date

import pytest
from semantic.tools import execute

from mcp_runner.trino import Trino, TrinoError


class FakeTrino(Trino):
    def __init__(self, rows=None, fail=False):
        self.rows = rows if rows is not None else []
        self.fail = fail
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params):
        self.executed.append((sql, tuple(params)))
        if self.fail:
            raise TrinoError("simulated outage")
        return self.rows


@pytest.fixture
def trino(monkeypatch):
    """Install a fake client and hand it back for assertions."""
    client = FakeTrino()
    monkeypatch.setattr(execute, "Trino", lambda: client)
    return client


def _query(**overrides):
    payload = {
        "metrics": ["grocery_spend_eur"],
        "time_range": {"grain": "month", "start": "2026-01-01", "end": "2026-03-31"},
    }
    payload.update(overrides)
    return execute.query(**payload)


class TestExclusions:
    def test_exclusions_ride_with_the_numbers(self, wired, trino):
        trino.rows = [["2026-01-01", 1234.56]]
        out = _query()
        assert "1234.56" in out
        assert "EXCLUDES" in out
        assert "Mercadona" in out

    def test_each_metric_states_its_own(self, wired, trino):
        trino.rows = [["2026-01-01", 10.0, 2]]
        out = _query(metrics=["grocery_spend_eur", "shopping_trips"])
        assert "grocery_spend_eur:" in out
        assert "shopping_trips:" in out

    def test_exclusions_survive_truncation(self, wired, trino, monkeypatch):
        """The one outcome this tool must never produce: numbers with no caveat.

        `truncate_lines` drops from the tail, so budgeting the whole reply at
        once would cut the exclusions first. The budget is pinned small so the
        guard holds whatever `MCP_BUDGET_BYTES` widens the working size to.
        """
        monkeypatch.setattr(execute, "QUERY_BUDGET", 1024)
        monkeypatch.setattr(execute, "_MIN_ROW_BUDGET", 256)
        trino.rows = [[f"2026-{month:02d}-01", 1000.0 + month] for month in range(1, 13)] * 40
        out = _query()
        assert "EXCLUDES" in out
        assert "Mercadona" in out
        assert "TRUNCATED" in out

    def test_stays_within_budget(self, wired, trino):
        trino.rows = [[f"2026-{month:02d}-01", 1000.0 + month] for month in range(1, 13)] * 40
        assert len(_query().encode()) <= execute.QUERY_BUDGET


class TestBoundParameters:
    def test_values_are_bound_not_interpolated(self, wired, trino):
        trino.rows = [["2026-01-01", 1.0]]
        _query(filters=[{"dimension": "supermarket", "op": "=", "value": "MERCADONA"}])
        sql, params = trino.executed[0]
        assert "?" in sql
        assert "MERCADONA" not in sql
        assert "MERCADONA" in params

    def test_window_dates_are_bound(self, wired, trino):
        trino.rows = [["2026-01-01", 1.0]]
        _query()
        _, params = trino.executed[0]
        assert date(2026, 1, 1) in params


class TestDegradation:
    def test_empty_result_is_an_answer(self, wired, trino):
        out = _query()
        assert "0 rows" in out
        assert "empty result rather than an error" in out

    def test_warehouse_failure_says_the_query_was_valid(self, wired, trino):
        trino.fail = True
        out = _query()
        assert out.startswith("WAREHOUSE ERROR")
        assert "simulated outage" in out

    def test_failure_leaks_no_sql_to_the_model(self, wired, trino):
        trino.fail = True
        out = _query()
        assert "SELECT" not in out

    def test_invalid_query_never_reaches_trino(self, wired, trino):
        out = _query(metrics=["no_such_metric"])
        assert out.startswith("INVALID")
        assert trino.executed == []

    def test_partial_period_is_stated_as_a_value(self, wired, trino):
        trino.rows = [["2026-01-01", 1.0]]
        out = _query(time_range={"grain": "month", "last": "3 months"})
        assert "PARTIAL PERIOD" in out

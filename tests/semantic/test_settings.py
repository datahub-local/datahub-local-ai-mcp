"""Scope configuration and the warehouse-backed `samples()`.

`settings` caches with `lru_cache`, so every test clears it - a leaked cache
entry makes a later test pass for the wrong reason.
"""

from __future__ import annotations

import pytest
from semantic import settings

from mcp_runner.trino import TrinoError


@pytest.fixture(autouse=True)
def _clear_caches():
    settings.registry.cache_clear()
    settings.warehouse.cache_clear()
    yield
    settings.registry.cache_clear()
    settings.warehouse.cache_clear()


class TestScopes:
    def test_parses_pairs(self, monkeypatch):
        monkeypatch.setenv("SEMANTIC_WAREHOUSE_SCOPES", "silver.bodega,gold.bodega")
        assert settings.scopes() == (("silver", "bodega"), ("gold", "bodega"))

    def test_tolerates_whitespace_and_trailing_comma(self, monkeypatch):
        monkeypatch.setenv("SEMANTIC_WAREHOUSE_SCOPES", " silver.bodega , gold.bodega , ")
        assert settings.scopes() == (("silver", "bodega"), ("gold", "bodega"))

    def test_unset_raises_naming_the_variable(self, monkeypatch):
        """A guessed catalog reads an empty information_schema, which surfaces as
        a broken registry rather than a missing setting."""
        monkeypatch.delenv("SEMANTIC_WAREHOUSE_SCOPES", raising=False)
        with pytest.raises(ValueError, match="SEMANTIC_WAREHOUSE_SCOPES"):
            settings.scopes()

    def test_malformed_entry_raises(self, monkeypatch):
        monkeypatch.setenv("SEMANTIC_WAREHOUSE_SCOPES", "silver")
        with pytest.raises(ValueError, match="not catalog.schema"):
            settings.scopes()


class _FakeWarehouse:
    def __init__(self, counts=None, values=None, fail_values=False):
        self._counts = counts if counts is not None else {}
        self._values = values or {}
        self.fail_values = fail_values
        self.value_calls: list[tuple[str, str]] = []

    def cardinality(self, table):
        return self._counts

    def values(self, table, column, limit):
        self.value_calls.append((table, column))
        if self.fail_values:
            raise TrinoError("simulated outage")
        return self._values.get(column, [])


class TestSamples:
    def _wire(self, monkeypatch, registry, warehouse):
        monkeypatch.setattr(settings, "registry", lambda: registry)
        monkeypatch.setattr(settings, "warehouse", lambda: warehouse)

    def test_time_dimensions_are_skipped(self, monkeypatch, registry):
        """list_dimensions sends time to time_range and shows no values for it,
        so querying one would cost a scan to produce output nothing reads."""
        warehouse = _FakeWarehouse(counts={"supermarket": 1})
        self._wire(monkeypatch, registry, warehouse)
        result = settings.samples("invoices")
        assert "invoice_date" not in result
        assert "supermarket" in result
        assert all(column != "invoice_date" for _, column in warehouse.value_calls)

    def test_cardinality_comes_from_stats(self, monkeypatch, registry):
        warehouse = _FakeWarehouse(
            counts={"supermarket": 1, "payment_method": 2},
            values={"supermarket": ["MERCADONA"]},
        )
        self._wire(monkeypatch, registry, warehouse)
        result = settings.samples("invoices")
        assert result["supermarket"]["cardinality"] == 1
        assert result["supermarket"]["values"] == ["MERCADONA"]

    def test_missing_stat_falls_back_to_the_values_read(self, monkeypatch, registry):
        """Iceberg does not always hold an NDV; the read values are then exact."""
        warehouse = _FakeWarehouse(counts={}, values={"supermarket": ["MERCADONA", "ALDI"]})
        self._wire(monkeypatch, registry, warehouse)
        assert settings.samples("invoices")["supermarket"]["cardinality"] == 2

    def test_zero_cardinality_skips_the_value_query(self, monkeypatch, registry):
        warehouse = _FakeWarehouse(counts={"supermarket": 0, "payment_method": 0})
        self._wire(monkeypatch, registry, warehouse)
        result = settings.samples("invoices")
        assert result["supermarket"] == {"cardinality": 0, "values": []}
        assert warehouse.value_calls == []

    def test_value_failure_degrades_that_dimension_only(self, monkeypatch, registry):
        warehouse = _FakeWarehouse(counts={"supermarket": 1}, fail_values=True)
        self._wire(monkeypatch, registry, warehouse)
        result = settings.samples("invoices")
        assert result["supermarket"]["values"] == []
        assert result["supermarket"]["cardinality"] == 1

    def test_unknown_model_is_empty(self, monkeypatch, registry):
        self._wire(monkeypatch, registry, _FakeWarehouse())
        assert settings.samples("nope") == {}
        assert settings.samples("") == {}

    def test_sample_values_drops_empty_dimensions(self, monkeypatch, registry):
        warehouse = _FakeWarehouse(
            counts={"supermarket": 1, "payment_method": 1},
            values={"supermarket": ["MERCADONA"]},
        )
        self._wire(monkeypatch, registry, warehouse)
        assert settings.sample_values("invoices") == {"supermarket": ["MERCADONA"]}

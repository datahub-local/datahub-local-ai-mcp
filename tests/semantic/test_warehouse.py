"""The warehouse-backed metadata source: SQL shape, caching, and degradation.

No Trino: a fake client records the statements it was asked to run, which is
what the assertions are about. The one thing these tests must protect is that a
failed refresh serves the previous value instead of failing the tool - that is
the property the ConfigMap sidecar used to provide by simply being a file.
"""

from __future__ import annotations

import pytest
from semantic.warehouse import Warehouse

from mcp_runner.trino import Trino, TrinoError, qualified


class FakeTrino(Trino):
    def __init__(self, responses=None, fail=False):
        self.statements: list[str] = []
        self.responses = responses or {}
        self.fail = fail
        self.calls = 0

    def query(self, sql: str):
        self.statements.append(sql)
        self.calls += 1
        if self.fail:
            raise TrinoError("simulated outage")
        for needle, rows in self.responses.items():
            if needle in sql:
                return rows
        return []


COLUMN_ROWS = [
    ["invoices", "invoice_number", "Natural primary key."],
    ["invoices", "total_amount", "Receipt total, VAT included."],
    ["invoices", "card_number_masked", None],
    ["stores", "store_id", "Store VAT identifier."],
]


def _warehouse(**kwargs) -> tuple[Warehouse, FakeTrino]:
    client = FakeTrino(**kwargs)
    return Warehouse(client=client, ttl=100.0), client


class TestManifest:
    def test_undocumented_columns_are_excluded(self):
        """A NULL comment is what reproduces 'undocumented' from the dbt manifest.

        The registry gate means "documented in schema.yml"; including a column
        with no comment would silently weaken it to "the column exists".
        """
        warehouse, _ = _warehouse(responses={"information_schema.columns": COLUMN_ROWS})
        nodes = warehouse.manifest([("silver", "bodega")])["nodes"]
        columns = nodes["model.bodega.invoices"]["columns"]
        assert set(columns) == {"invoice_number", "total_amount"}
        assert "card_number_masked" not in columns

    def test_blank_comment_is_also_excluded(self):
        rows = [["invoices", "supermarket", "   "]]
        warehouse, _ = _warehouse(responses={"information_schema.columns": rows})
        nodes = warehouse.manifest([("silver", "bodega")])["nodes"]
        assert nodes["model.bodega.invoices"]["columns"] == {}

    def test_shape_matches_what_bind_tables_expects(self):
        warehouse, _ = _warehouse(responses={"information_schema.columns": COLUMN_ROWS})
        node = warehouse.manifest([("silver", "bodega")])["nodes"]["model.bodega.invoices"]
        assert node["resource_type"] == "model"
        assert node["database"] == "silver"
        assert node["schema"] == "bodega"
        assert node["alias"] == "invoices"
        assert f"{node['database']}.{node['schema']}.{node['alias']}" == "silver.bodega.invoices"

    def test_reads_every_scope(self):
        warehouse, client = _warehouse(responses={"information_schema.columns": COLUMN_ROWS})
        warehouse.manifest([("silver", "bodega"), ("gold", "bodega")])
        assert len(client.statements) == 2
        assert any("silver.information_schema" in s for s in client.statements)
        assert any("gold.information_schema" in s for s in client.statements)


class TestCaching:
    def test_second_call_within_ttl_does_not_requery(self):
        warehouse, client = _warehouse(responses={"SHOW STATS": [["a", None, 3.0, 0.0, None, None, None]]})
        warehouse.cardinality("silver.bodega.invoices")
        warehouse.cardinality("silver.bodega.invoices")
        assert client.calls == 1

    def test_expired_entry_is_refreshed(self):
        client = FakeTrino(responses={"SHOW STATS": [["a", None, 3.0, 0.0, None, None, None]]})
        warehouse = Warehouse(client=client, ttl=0.0)
        warehouse.cardinality("silver.bodega.invoices")
        warehouse.cardinality("silver.bodega.invoices")
        assert client.calls == 2

    def test_failure_serves_the_stale_value(self):
        """The property the sidecar file used to give for free: an outage costs
        freshness, not the tool."""
        client = FakeTrino(responses={"SHOW STATS": [["a", None, 3.0, 0.0, None, None, None]]})
        warehouse = Warehouse(client=client, ttl=0.0)
        assert warehouse.cardinality("silver.bodega.invoices") == {"a": 3}
        client.fail = True
        assert warehouse.cardinality("silver.bodega.invoices") == {"a": 3}

    def test_cold_failure_raises(self):
        """With nothing cached there is no honest answer, so it must not invent one."""
        warehouse, _ = _warehouse(fail=True)
        with pytest.raises(TrinoError):
            warehouse.cardinality("silver.bodega.invoices")

    def test_distinct_keys_do_not_collide(self):
        warehouse, client = _warehouse(responses={"SELECT": [["A"], ["B"]]})
        warehouse.values("silver.bodega.invoices", "supermarket", 10)
        warehouse.values("silver.bodega.invoices", "payment_method", 10)
        assert client.calls == 2


class TestStatements:
    def test_top_values_orders_by_frequency(self):
        warehouse, client = _warehouse(responses={"SELECT": [["MERCADONA"]]})
        warehouse.values("silver.bodega.invoices", "supermarket", 12)
        sql = client.statements[0]
        assert "ORDER BY count(*) DESC" in sql
        assert "IS NOT NULL" in sql
        assert "LIMIT 12" in sql

    def test_cardinality_reads_iceberg_stats_not_a_scan(self):
        warehouse, client = _warehouse(responses={"SHOW STATS": []})
        warehouse.cardinality("silver.bodega.invoices")
        assert client.statements[0] == "SHOW STATS FOR silver.bodega.invoices"

    def test_stats_row_without_a_count_is_skipped(self):
        rows = [
            ["invoice_number", 1134.0, 75.0, 0.0, None, None, None],
            ["invoice_date", None, None, 0.0, None, "2026-01-05", "2026-08-19"],
            [None, None, None, None, 75.0, None, None],
        ]
        warehouse, _ = _warehouse(responses={"SHOW STATS": rows})
        assert warehouse.cardinality("silver.bodega.invoices") == {"invoice_number": 75}


class TestIdentifierGuard:
    """Trino has no bind parameters for identifiers, so names are interpolated.
    The guard is what keeps that from being a hole if a bad name ever reaches it.
    """

    @pytest.mark.parametrize(
        "table",
        [
            "silver.bodega.invoices; DROP TABLE x",
            "silver.bodega",
            "silver.bodega.in voices",
            "silver.bodega.invoices--",
        ],
    )
    def test_bad_table_refused(self, table):
        with pytest.raises(TrinoError):
            qualified(table)

    def test_good_table_passes(self):
        assert qualified("silver.bodega.invoices") == "silver.bodega.invoices"

    def test_bad_column_refused(self):
        warehouse, _ = _warehouse()
        with pytest.raises(TrinoError):
            warehouse.values("silver.bodega.invoices", "a; DROP TABLE x", 10)

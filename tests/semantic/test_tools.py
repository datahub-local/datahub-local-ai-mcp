"""The discovery tools: what they say, and that they stay inside budget.

Byte budgets are not caution: one ~16KB tool result reproducibly ends a run with
`terminal turn had empty text`, which delivers nothing at all.
"""

from __future__ import annotations

import json

from semantic import register
from semantic.tools import discovery

from mcp_runner.server import Registry as ToolRegistry


class TestListMetrics:
    def test_lists_every_metric_with_its_exclusions(self, wired):
        out = discovery.list_metrics()
        assert "grocery_spend_eur" in out
        assert "blended_unit_price_eur" in out
        assert "excludes:" in out
        assert "registry testsha" in out

    def test_search_filters(self, wired):
        out = discovery.list_metrics(search="basket")
        assert "avg_basket_eur" in out
        assert "category_spend_eur" not in out

    def test_no_match_names_what_is_available(self, wired):
        out = discovery.list_metrics(search="electricity")
        assert "no metric matches" in out
        assert "grocery_spend_eur" in out

    def test_within_budget(self, wired):
        assert len(discovery.list_metrics().encode()) <= discovery.LIST_BUDGET


class TestDescribeMetric:
    def test_names_its_source_table(self, wired):
        """§6.2: seeing the source model weakens the pull toward re-deriving it in SQL."""
        out = discovery.describe_metric("category_spend_eur")
        assert "gold.bodega.category_spending" in out

    def test_leads_with_the_exclusions(self, wired):
        out = discovery.describe_metric("grocery_spend_eur")
        assert "EXCLUDES:" in out
        assert "Cash purchases are absent" in out

    def test_states_the_grain_floor(self, wired):
        out = discovery.describe_metric("avg_basket_eur")
        assert "finest time grain: week" in out

    def test_carries_no_formula(self, wired):
        """The agent never needs the expression, so it never sees one."""
        out = discovery.describe_metric("blended_unit_price_eur")
        assert "nullif" not in out
        assert "sum(" not in out

    def test_offers_a_runnable_example(self, wired):
        out = discovery.describe_metric("category_spend_eur")
        example = out.split("example: ")[1].strip()
        assert json.loads(example)["metrics"] == ["category_spend_eur"]

    def test_unknown_metric_suggests_a_near_miss(self, wired):
        assert "did you mean 'grocery_spend_eur'" in discovery.describe_metric("grocery_spnd_eur")

    def test_within_budget(self, wired):
        for name in ("grocery_spend_eur", "blended_unit_price_eur", "avg_basket_eur"):
            assert len(discovery.describe_metric(name).encode()) <= discovery.DESCRIBE_BUDGET


class TestListDimensions:
    def test_shows_samples_where_known(self, wired):
        out = discovery.list_dimensions("grocery_spend_eur")
        assert "supermarket" in out
        assert "MERCADONA" in out

    def test_says_filter_time_with_time_range(self, wired):
        out = discovery.list_dimensions("grocery_spend_eur")
        assert "filter with time_range" in out

    def test_tells_the_model_to_copy_a_value_rather_than_type_one(self, wired):
        """No single casing rule is correct: `category` is UPPERCASE on the same
        model where `subcategory` is LLM-written mixed case."""
        out = discovery.list_dimensions("grocery_spend_eur")
        assert "EXACTLY as shown" in out
        assert "Copy a value" in out

    def test_missing_samples_degrade_to_names_only(self, wired):
        """Samples need a warehouse, so their absence must fail nothing."""
        out = discovery.list_dimensions("blended_unit_price_eur")
        assert "description_clean" in out
        assert "sample values unavailable" in out

    def test_within_budget(self, wired):
        assert len(discovery.list_dimensions("grocery_spend_eur").encode()) <= discovery.DIMENSIONS_BUDGET


class TestExplain:
    def test_returns_sql_and_the_resolved_window(self, wired):
        out = discovery.explain(
            metrics=["grocery_spend_eur"], time_range={"grain": "month", "last": "3 months"}
        )
        assert "SELECT" in out
        assert "silver.bodega.invoices" in out
        assert "window:" in out
        assert "not executed" in out

    def test_does_not_execute(self, wired):
        """No warehouse is configured in these tests; explain must still answer."""
        out = discovery.explain(
            metrics=["category_spend_eur"], time_range={"grain": "month", "last": "6 months"}
        )
        assert "gold.bodega.category_spending" in out

    def test_flags_a_partial_period(self, wired):
        out = discovery.explain(
            metrics=["grocery_spend_eur"], time_range={"grain": "month", "last": "3 months"}
        )
        assert "partial period:" in out

    def test_invalid_query_returns_readable_text_not_an_exception(self, wired):
        """Readable text lets the model retry; a protocol error ends the run."""
        out = discovery.explain(
            metrics=["electricity_spend"], time_range={"grain": "month", "last": "1 month"}
        )
        assert out.startswith("INVALID:")
        assert "no metric named" in out

    def test_undeclared_filter_is_refused(self, wired):
        out = discovery.explain(
            metrics=["grocery_spend_eur"],
            time_range={"grain": "month", "last": "1 month"},
            filters=[{"dimension": "nonexistent", "op": "=", "value": "x"}],
        )
        assert out.startswith("INVALID:")

    def test_within_budget(self, wired):
        out = discovery.explain(
            metrics=["category_spend_eur"],
            time_range={"grain": "month", "last": "12 months"},
            group_by=["category"],
        )
        assert len(out.encode()) <= discovery.EXPLAIN_BUDGET


class TestRegistration:
    def test_registers_exactly_the_five_tools(self):
        tools = ToolRegistry()
        register(tools)
        assert set(tools.tools) == {
            "list_metrics",
            "describe_metric",
            "list_dimensions",
            "explain",
            "query",
        }

    def test_no_sql_tool_exists(self):
        """Not gated, not approval-wrapped - absent."""
        tools = ToolRegistry()
        register(tools)
        for name in tools.tools:
            assert "sql" not in name.lower()
            assert "execute" not in name.lower()

    def test_every_tool_declares_a_budget_under_4kb(self):
        tools = ToolRegistry()
        register(tools)
        for tool in tools.tools.values():
            assert 0 < tool.budget <= 4096

    def test_schemas_reject_unknown_arguments(self):
        tools = ToolRegistry()
        register(tools)
        for tool in tools.tools.values():
            assert tool.schema.get("additionalProperties") is False

    def test_a_tool_error_is_returned_as_text(self, wired):
        """The runner folds a raised error into readable content rather than failing the run."""
        tools = ToolRegistry()
        register(tools)
        assert tools.call("describe_metric", {"name": "nope"}).startswith("no metric named")

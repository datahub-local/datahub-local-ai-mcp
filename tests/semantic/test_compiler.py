"""What SQL the compiler actually emits.

These are the assertions that replaced prose rules, the same move
`tests/test_expressions.py` made for PromQL: the ratio direction, the parameter
binding and the allowlisted `order_by` are checked against the emitted string,
not against a sentence in a prompt telling a model to get them right.
"""

from __future__ import annotations

from datetime import date

import pytest
from semantic.compiler import compile_query, inline_sql
from semantic.query import parse, validate


def _compile(payload, registry, today=date(2026, 9, 4)):
    query = parse(payload)
    validate(query, registry)
    return compile_query(query, registry, today=today)


class TestRatioDirection:
    """A ratio is sum/sum at the group grain, never an average of per-row ratios.

    Measured on the live data: avg(unit_price) = 2.5099 where the metric is
    2.3850, 5.2% apart. top_products.sql computes the first; only the second is
    the metric.
    """

    def test_ratio_is_sum_over_sum(self, registry):
        compiled = _compile(
            {
                "metrics": ["blended_unit_price_eur"],
                "time_range": {"grain": "week", "last": "6 weeks"},
            },
            registry,
        )
        assert "sum(total_amount) / nullif(sum(quantity), 0)" in compiled.sql

    def test_ratio_never_averages_a_per_row_ratio(self, registry):
        compiled = _compile(
            {
                "metrics": ["blended_unit_price_eur"],
                "time_range": {"grain": "week", "last": "6 weeks"},
            },
            registry,
        )
        assert "avg(" not in compiled.sql.lower()
        # The `unit_price` column is never read - only the metric's own output
        # alias contains that substring.
        select_clause = compiled.sql.split(" FROM ")[0]
        assert "unit_price)" not in select_clause
        assert 'AS "blended_unit_price_eur"' in select_clause

    def test_denominator_is_nullif_guarded(self, registry):
        """One zero-quantity row must not fail the whole query."""
        compiled = _compile(
            {"metrics": ["avg_basket_eur"], "time_range": {"grain": "week", "last": "4 weeks"}},
            registry,
        )
        assert "nullif(count(invoice_number), 0)" in compiled.sql


class TestRounding:
    """Binary float noise reaches Slack verbatim, so it is rounded in SQL.

    Live: `sum(total_spent)` for FRUITS_VEGETABLES returns 465.0999999999997
    where the receipts say 465.10. The prompt requires transcribing a returned
    value, so the value has to arrive already correct.
    """

    def test_a_sum_is_rounded_to_cents(self, registry):
        compiled = _compile(
            {"metrics": ["grocery_spend_eur"], "time_range": {"grain": "month", "last": "3 months"}},
            registry,
        )
        assert "round(sum(total_amount), 2)" in compiled.sql

    def test_a_ratio_keeps_four_places(self, registry):
        compiled = _compile(
            {
                "metrics": ["blended_unit_price_eur"],
                "time_range": {"grain": "week", "last": "6 weeks"},
            },
            registry,
        )
        assert "round(sum(total_amount) / nullif(sum(quantity), 0), 4)" in compiled.sql

    def test_a_count_is_not_rounded(self, registry):
        """Rounding an integer only invites a decimal point."""
        compiled = _compile(
            {"metrics": ["shopping_trips"], "time_range": {"grain": "day", "last": "7 days"}},
            registry,
        )
        assert 'count(invoice_number) AS "shopping_trips"' in compiled.sql
        assert "round(count" not in compiled.sql


class TestParameterBinding:
    def test_filter_values_are_bound_never_interpolated(self, registry):
        compiled = _compile(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "last": "3 months"},
                "filters": [{"dimension": "supermarket", "op": "=", "value": "MERCADONA"}],
            },
            registry,
        )
        assert "supermarket = ?" in compiled.sql
        assert "MERCADONA" not in compiled.sql
        assert "MERCADONA" in compiled.params

    def test_injection_attempt_stays_a_parameter(self, registry):
        """A quote in a value is data. It never reaches the statement text."""
        evil = "MERCADONA'; DROP TABLE invoices; --"
        compiled = _compile(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "last": "3 months"},
                "filters": [{"dimension": "supermarket", "op": "=", "value": evil}],
            },
            registry,
        )
        assert "DROP TABLE" not in compiled.sql
        assert evil in compiled.params
        assert compiled.sql.count("?") == len(compiled.params)

    def test_contains_builds_the_pattern_and_binds_the_value(self, registry):
        compiled = _compile(
            {
                "metrics": ["blended_unit_price_eur"],
                "time_range": {"grain": "week", "last": "6 weeks"},
                "filters": [{"dimension": "description_clean", "op": "contains", "value": "LECHE"}],
            },
            registry,
        )
        assert "description_clean LIKE ?" in compiled.sql
        assert "%LECHE%" in compiled.params

    def test_in_binds_one_placeholder_per_value(self, registry):
        compiled = _compile(
            {
                "metrics": ["category_spend_eur"],
                "time_range": {"grain": "month", "last": "3 months"},
                "filters": [
                    {"dimension": "category", "op": "in", "value": ["DAIRY_EGGS", "BEVERAGES"]}
                ],
            },
            registry,
        )
        assert "category IN (?, ?)" in compiled.sql
        assert compiled.sql.count("?") == len(compiled.params)

    def test_window_bounds_are_bound_and_half_open(self, registry):
        compiled = _compile(
            {"metrics": ["grocery_spend_eur"], "time_range": {"grain": "day", "last": "7 days"}},
            registry,
        )
        assert "invoice_date >= ?" in compiled.sql
        assert "invoice_date < ?" in compiled.sql
        start, end = compiled.window
        assert (start, end) == (date(2026, 8, 29), date(2026, 9, 5))


class TestOrderBy:
    def test_order_by_compiles_to_a_position_not_a_name(self, registry):
        """No request-supplied identifier reaches the ORDER BY clause."""
        compiled = _compile(
            {
                "metrics": ["category_spend_eur"],
                "time_range": {"grain": "month", "last": "3 months"},
                "group_by": ["category"],
                "order_by": "category_spend_eur",
            },
            registry,
        )
        assert "ORDER BY 3 DESC" in compiled.sql

    def test_unprojected_order_by_is_rejected_before_compiling(self, registry):
        from semantic.query import QueryError

        query = parse(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "last": "3 months"},
                "order_by": "total_amount; DROP TABLE invoices",
            }
        )
        with pytest.raises(QueryError, match="not a projected column"):
            validate(query, registry)


class TestPartialPeriod:
    def test_current_month_is_flagged_with_its_bucket(self, registry):
        compiled = _compile(
            {"metrics": ["grocery_spend_eur"], "time_range": {"grain": "month", "last": "3 months"}},
            registry,
            today=date(2026, 9, 4),
        )
        assert compiled.is_partial_period
        assert compiled.partial_bucket == "2026-09"

    def test_a_closed_past_window_is_not_flagged(self, registry):
        compiled = _compile(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "start": "2026-01-01", "end": "2026-08-31"},
            },
            registry,
            today=date(2026, 9, 4),
        )
        assert not compiled.is_partial_period
        assert compiled.partial_bucket is None

    def test_week_bucket_is_named_by_its_monday(self, registry):
        compiled = _compile(
            {"metrics": ["avg_basket_eur"], "time_range": {"grain": "week", "last": "4 weeks"}},
            registry,
            today=date(2026, 9, 4),
        )
        assert compiled.partial_bucket == "week of 2026-08-31"


class TestShape:
    def test_table_comes_from_the_manifest(self, registry):
        compiled = _compile(
            {"metrics": ["category_spend_eur"], "time_range": {"grain": "month", "last": "3 months"}},
            registry,
        )
        assert compiled.table == "gold.bodega.category_spending"
        assert "FROM gold.bodega.category_spending" in compiled.sql

    def test_group_by_is_positional_and_covers_period_plus_dimensions(self, registry):
        compiled = _compile(
            {
                "metrics": ["category_spend_eur"],
                "time_range": {"grain": "month", "last": "3 months"},
                "group_by": ["category"],
            },
            registry,
        )
        assert "GROUP BY 1, 2" in compiled.sql
        assert compiled.projection == ("period", "category", "category_spend_eur")

    def test_limit_is_cast_into_the_statement(self, registry):
        compiled = _compile(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "day", "last": "30 days"},
                "limit": 50,
            },
            registry,
        )
        assert compiled.sql.endswith("LIMIT 50")

    def test_time_bucket_uses_the_requested_grain(self, registry):
        compiled = _compile(
            {"metrics": ["grocery_spend_eur"], "time_range": {"grain": "month", "last": "6 months"}},
            registry,
        )
        assert "date_trunc('month', invoice_date) AS period" in compiled.sql


class TestInlineSql:
    def test_inline_is_display_only_and_quotes_are_escaped(self, registry):
        compiled = _compile(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "last": "3 months"},
                "filters": [{"dimension": "supermarket", "op": "=", "value": "O'HARA"}],
            },
            registry,
        )
        rendered = inline_sql(compiled)
        assert "'O''HARA'" in rendered
        assert "?" not in rendered
        # The parameterised form is what executes; inlining changes nothing.
        assert compiled.sql.count("?") == len(compiled.params)

    def test_dates_render_as_date_literals(self, registry):
        compiled = _compile(
            {"metrics": ["grocery_spend_eur"], "time_range": {"grain": "day", "last": "7 days"}},
            registry,
        )
        assert "DATE '2026-08-29'" in inline_sql(compiled)

"""G2: an undeclared filter or metric is a validation error, not a wrong number.

Also the registry rules that make a bad definition unloadable, which is the same
gate `semantic-compile` applies in CI - one module, so the two cannot drift.
"""

from __future__ import annotations

import textwrap
from datetime import date

import pytest
from semantic import registry as registry_module
from semantic.query import QueryError, parse, resolve_window, validate
from semantic.registry import RegistryError, validate_registry


class TestUndeclared:
    def test_unknown_metric_is_rejected(self, registry):
        query = parse({"metrics": ["electricity_spend"], "time_range": {"grain": "month", "last": "1 month"}})
        with pytest.raises(QueryError, match="no metric named 'electricity_spend'"):
            validate(query, registry)

    def test_unknown_dimension_is_rejected(self, registry):
        query = parse(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "last": "1 month"},
                "group_by": ["store_city"],
            }
        )
        with pytest.raises(QueryError, match="no dimension on invoices named 'store_city'"):
            validate(query, registry)

    def test_dimension_from_another_model_is_rejected(self, registry):
        """`category` exists, but not on the model grocery_spend_eur reads."""
        query = parse(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "last": "1 month"},
                "filters": [{"dimension": "category", "op": "=", "value": "DAIRY_EGGS"}],
            }
        )
        with pytest.raises(QueryError, match="no dimension on invoices named 'category'"):
            validate(query, registry)

    def test_metrics_spanning_two_models_are_rejected_by_name(self, registry):
        query = parse(
            {
                "metrics": ["grocery_spend_eur", "category_spend_eur"],
                "time_range": {"grain": "month", "last": "1 month"},
            }
        )
        with pytest.raises(QueryError, match="span models category_spending, invoices"):
            validate(query, registry)


class TestNearMiss:
    """A near-miss suggestion turns a silent empty result into a corrected retry.

    description_clean and store_name are trim(upper(...)) in silver, so a model
    filtering on mixed case matches nothing at all.
    """

    def test_misspelled_value_suggests_the_real_one(self, registry, samples):
        query = parse(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "last": "1 month"},
                "filters": [{"dimension": "supermarket", "op": "=", "value": "Mercadonna"}],
            }
        )
        with pytest.raises(QueryError, match='did you mean "MERCADONA"'):
            validate(query, registry, samples)

    def test_wrong_case_suggests_the_upper_case_value(self, registry, samples):
        query = parse(
            {
                "metrics": ["category_spend_eur"],
                "time_range": {"grain": "month", "last": "1 month"},
                "filters": [{"dimension": "category", "op": "=", "value": "Dairy_Eggs"}],
            }
        )
        with pytest.raises(QueryError, match='did you mean "DAIRY_EGGS"'):
            validate(query, registry, samples)

    def test_unrelated_value_lists_known_values(self, registry, samples):
        query = parse(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "last": "1 month"},
                "filters": [{"dimension": "supermarket", "op": "=", "value": "LIDL"}],
            }
        )
        with pytest.raises(QueryError, match="Known values include: MERCADONA"):
            validate(query, registry, samples)

    def test_mixed_case_stored_values_are_matched_too(self, registry):
        """`subcategory` is LLM-written mixed case, so upper() is not the rule."""
        query = parse(
            {
                "metrics": ["category_spend_eur"],
                "time_range": {"grain": "month", "last": "1 month"},
                "filters": [{"dimension": "category", "op": "=", "value": "whole milk"}],
            }
        )
        with pytest.raises(QueryError, match='did you mean "Whole milk"'):
            validate(query, registry, {"category": ["Whole milk", "Banana"]})

    def test_misspelled_metric_suggests_the_real_one(self, registry):
        query = parse({"metrics": ["grocery_spend"], "time_range": {"grain": "month", "last": "1 month"}})
        with pytest.raises(QueryError, match="did you mean 'grocery_spend_eur'"):
            validate(query, registry)

    def test_no_samples_means_no_value_check(self, registry):
        """Absent samples degrade validation; they never fail a valid query."""
        query = parse(
            {
                "metrics": ["grocery_spend_eur"],
                "time_range": {"grain": "month", "last": "1 month"},
                "filters": [{"dimension": "supermarket", "op": "=", "value": "ANYTHING"}],
            }
        )
        validate(query, registry, {})


class TestGrainFloor:
    def test_grain_finer_than_grain_min_is_rejected(self, registry):
        query = parse({"metrics": ["avg_basket_eur"], "time_range": {"grain": "day", "last": "30 days"}})
        with pytest.raises(QueryError, match="finer than 'avg_basket_eur' allows"):
            validate(query, registry)

    def test_grain_at_the_floor_is_allowed(self, registry):
        query = parse({"metrics": ["avg_basket_eur"], "time_range": {"grain": "week", "last": "8 weeks"}})
        validate(query, registry)

    def test_the_coarsest_floor_wins_across_metrics(self, registry):
        query = parse(
            {
                "metrics": ["grocery_spend_eur", "avg_basket_eur"],
                "time_range": {"grain": "day", "last": "30 days"},
            }
        )
        with pytest.raises(QueryError, match="grain_min 'week'"):
            validate(query, registry)


class TestQueryShape:
    def test_time_range_is_required(self):
        with pytest.raises(QueryError, match="time_range is required"):
            parse({"metrics": ["grocery_spend_eur"]})

    def test_at_least_one_metric_is_required(self):
        with pytest.raises(QueryError, match="name at least one metric"):
            parse({"metrics": [], "time_range": {"grain": "day", "last": "7 days"}})

    def test_too_many_metrics_is_rejected(self):
        with pytest.raises(QueryError, match="at most 5"):
            parse({"metrics": ["a"] * 6, "time_range": {"grain": "day", "last": "7 days"}})

    def test_limit_above_the_cap_is_rejected(self):
        with pytest.raises(QueryError, match="between 1 and 1000"):
            parse({"metrics": ["a"], "time_range": {"grain": "day", "last": "7 days"}, "limit": 5000})

    def test_unknown_op_is_rejected(self):
        with pytest.raises(QueryError, match="is not one of"):
            parse(
                {
                    "metrics": ["a"],
                    "time_range": {"grain": "day", "last": "7 days"},
                    "filters": [{"dimension": "supermarket", "op": "LIKE", "value": "x"}],
                }
            )

    def test_in_needs_a_list(self, registry):
        query = parse(
            {
                "metrics": ["category_spend_eur"],
                "time_range": {"grain": "month", "last": "3 months"},
                "filters": [{"dimension": "category", "op": "in", "value": "DAIRY_EGGS"}],
            }
        )
        with pytest.raises(QueryError, match="needs a non-empty list"):
            validate(query, registry)

    def test_ordered_op_on_a_categorical_is_rejected(self, registry):
        query = parse(
            {
                "metrics": ["category_spend_eur"],
                "time_range": {"grain": "month", "last": "3 months"},
                "filters": [{"dimension": "category", "op": ">", "value": "D"}],
            }
        )
        with pytest.raises(QueryError, match="needs an ordered dimension"):
            validate(query, registry)

    def test_bad_last_expression_names_the_format(self):
        with pytest.raises(QueryError, match='Use "<n> <unit>"'):
            resolve_window(parse({"metrics": ["a"], "time_range": {"grain": "day", "last": "recently"}}).time_range)


class TestWindows:
    def test_last_n_months_is_inclusive_of_today(self):
        query = parse({"metrics": ["a"], "time_range": {"grain": "month", "last": "3 months"}})
        start, end = resolve_window(query.time_range, date(2026, 9, 4))
        assert (start, end) == (date(2026, 6, 5), date(2026, 9, 5))

    def test_last_one_month_is_a_trailing_month_not_a_single_day(self):
        """Regression: this resolved to a 1-day window, so "how much did I spend
        on dairy last month" silently answered with one day of data."""
        query = parse({"metrics": ["a"], "time_range": {"grain": "month", "last": "1 month"}})
        start, end = resolve_window(query.time_range, date(2026, 9, 4))
        assert (start, end) == (date(2026, 8, 5), date(2026, 9, 5))
        assert (end - start).days == 31

    def test_window_lengths_are_what_they_say(self):
        for last, days in [("7 days", 7), ("4 weeks", 28), ("12 months", 365), ("1 year", 365)]:
            query = parse({"metrics": ["a"], "time_range": {"grain": "day", "last": last}})
            start, end = resolve_window(query.time_range, date(2026, 9, 4))
            assert (end - start).days == days, last

    def test_month_end_does_not_overflow(self):
        """31 March minus one month has no 31 February to land on."""
        query = parse({"metrics": ["a"], "time_range": {"grain": "month", "last": "1 month"}})
        assert resolve_window(query.time_range, date(2026, 3, 31)) == (
            date(2026, 3, 1),
            date(2026, 4, 1),
        )

    def test_explicit_end_is_inclusive_in_the_request_and_exclusive_in_sql(self):
        query = parse(
            {"metrics": ["a"], "time_range": {"grain": "day", "start": "2026-01-01", "end": "2026-01-31"}}
        )
        assert resolve_window(query.time_range, date(2026, 9, 4)) == (date(2026, 1, 1), date(2026, 2, 1))

    def test_start_after_end_is_rejected(self):
        query = parse(
            {"metrics": ["a"], "time_range": {"grain": "day", "start": "2026-05-01", "end": "2026-01-01"}}
        )
        with pytest.raises(QueryError, match="is not before end"):
            resolve_window(query.time_range, date(2026, 9, 4))

    def test_month_arithmetic_crosses_a_year_boundary(self):
        query = parse({"metrics": ["a"], "time_range": {"grain": "month", "last": "6 months"}})
        start, end = resolve_window(query.time_range, date(2026, 2, 15))
        assert (start, end) == (date(2025, 8, 16), date(2026, 2, 16))
        assert (end - start).days == 184


class TestRegistryRules:
    """The rules `semantic-compile` gates on, applied by the same module."""

    def _load(self, tmp_path, yaml_text, manifest):
        path = tmp_path / "r.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return registry_module.load(path, manifest=manifest)

    def test_missing_excludes_fails_to_load(self, tmp_path, manifest):
        with pytest.raises(RegistryError, match="meta.excludes is mandatory"):
            self._load(
                tmp_path,
                """
                semantic_models:
                  - name: invoices
                    model: ref('invoices')
                    defaults: {agg_time_dimension: invoice_date}
                    dimensions:
                      - {name: invoice_date, type: time, type_params: {time_granularity: day}}
                    measures:
                      - {name: basket_amount, agg: sum, expr: total_amount}
                metrics:
                  - name: no_excludes
                    type: simple
                    type_params: {measure: basket_amount}
                    meta: {owner: alvaro, grain_min: day, description: something}
                """,
                manifest,
            )

    def test_avg_over_a_pre_aggregated_total_fails(self, tmp_path, manifest):
        """total_spent is already a SUM; the mean of daily sums is not a mean of anything."""
        with pytest.raises(RegistryError, match="average of sums"):
            self._load(
                tmp_path,
                """
                semantic_models:
                  - name: category_spending
                    model: ref('category_spending')
                    pre_aggregated: true
                    defaults: {agg_time_dimension: invoice_date}
                    dimensions:
                      - {name: invoice_date, type: time, type_params: {time_granularity: day}}
                    measures:
                      - {name: bad, agg: avg, expr: total_spent}
                metrics:
                  - name: m
                    type: simple
                    type_params: {measure: bad}
                    meta: {owner: a, grain_min: day, description: d, excludes: e}
                """,
                manifest,
            )

    def test_sql_fragment_in_an_expr_fails(self, tmp_path, manifest):
        with pytest.raises(RegistryError, match="not a bare column reference"):
            self._load(
                tmp_path,
                """
                semantic_models:
                  - name: invoices
                    model: ref('invoices')
                    defaults: {agg_time_dimension: invoice_date}
                    dimensions:
                      - {name: invoice_date, type: time, type_params: {time_granularity: day}}
                    measures:
                      - {name: sneaky, agg: sum, expr: "total_amount) FROM x --"}
                metrics:
                  - name: m
                    type: simple
                    type_params: {measure: sneaky}
                    meta: {owner: a, grain_min: day, description: d, excludes: e}
                """,
                manifest,
            )

    def test_unknown_column_fails_against_the_manifest(self, tmp_path, manifest):
        """G3: renaming a column dbt builds breaks the registry that references it."""
        with pytest.raises(RegistryError, match="does not have"):
            self._load(
                tmp_path,
                """
                semantic_models:
                  - name: invoices
                    model: ref('invoices')
                    defaults: {agg_time_dimension: invoice_date}
                    dimensions:
                      - {name: invoice_date, type: time, type_params: {time_granularity: day}}
                    measures:
                      - {name: gone, agg: sum, expr: total_amount_renamed}
                metrics:
                  - name: m
                    type: simple
                    type_params: {measure: gone}
                    meta: {owner: a, grain_min: day, description: d, excludes: e}
                """,
                manifest,
            )

    def test_unknown_ref_fails(self, tmp_path, manifest):
        with pytest.raises(RegistryError, match="matches no dbt model"):
            self._load(
                tmp_path,
                """
                semantic_models:
                  - name: ghost
                    model: ref('no_such_model')
                    defaults: {agg_time_dimension: invoice_date}
                    dimensions:
                      - {name: invoice_date, type: time, type_params: {time_granularity: day}}
                    measures:
                      - {name: m1, agg: sum, expr: total_amount}
                metrics:
                  - name: m
                    type: simple
                    type_params: {measure: m1}
                    meta: {owner: a, grain_min: day, description: d, excludes: e}
                """,
                manifest,
            )

    def test_duplicate_metric_name_is_reported(self, registry, manifest):
        raw = {
            "metrics": [
                {"name": "dupe", "type": "simple"},
                {"name": "dupe", "type": "simple"},
            ]
        }
        assert any("more than once" in problem for problem in validate_registry(registry, raw, None))

    def test_every_problem_is_reported_not_just_the_first(self, tmp_path, manifest):
        """A gate that reports one error per run is a queue, not a gate."""
        path = tmp_path / "r.yaml"
        path.write_text(
            textwrap.dedent(
                """
                semantic_models:
                  - name: invoices
                    model: ref('invoices')
                    defaults: {agg_time_dimension: invoice_date}
                    dimensions:
                      - {name: invoice_date, type: time, type_params: {time_granularity: day}}
                    measures:
                      - {name: a, agg: sum, expr: nope_one}
                      - {name: b, agg: sum, expr: nope_two}
                metrics:
                  - name: m
                    type: simple
                    type_params: {measure: a}
                    meta: {owner: x, grain_min: day, description: d}
                """
            )
        )
        with pytest.raises(RegistryError) as caught:
            registry_module.load(path, manifest=manifest)
        message = str(caught.value)
        assert "nope_one" in message and "nope_two" in message and "excludes" in message

    def test_the_real_registry_loads_and_binds(self, registry):
        assert registry.version == "testsha"
        assert registry.models["invoices"].table == "silver.bodega.invoices"
        assert registry.models["category_spending"].table == "gold.bodega.category_spending"

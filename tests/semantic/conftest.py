"""A registry and a manifest built in code, so the tests need no dbt and no cluster."""

from __future__ import annotations

import textwrap

import pytest
from semantic import registry as registry_module

REGISTRY_YAML = textwrap.dedent(
    """
    semantic_models:
      - name: invoices
        description: One row per shopping trip.
        model: ref('invoices')
        defaults:
          agg_time_dimension: invoice_date
        entities:
          - {name: invoice, type: primary, expr: invoice_number}
        dimensions:
          - name: invoice_date
            type: time
            type_params: {time_granularity: day}
          - {name: supermarket, type: categorical}
          - {name: payment_method, type: categorical}
        measures:
          - {name: basket_amount, agg: sum, expr: total_amount}
          - {name: basket_count, agg: count, expr: invoice_number}

      - name: invoice_items
        description: One row per product per invoice.
        model: ref('invoice_items')
        defaults:
          agg_time_dimension: invoice_date
        dimensions:
          - name: invoice_date
            type: time
            type_params: {time_granularity: day}
          - {name: description_clean, type: categorical}
        measures:
          - {name: line_amount, agg: sum, expr: total_amount}
          - {name: line_quantity, agg: sum, expr: quantity}

      - name: category_spending
        description: Daily spend by category.
        model: ref('category_spending')
        pre_aggregated: true
        defaults:
          agg_time_dimension: invoice_date
        dimensions:
          - name: invoice_date
            type: time
            type_params: {time_granularity: day}
          - {name: category, type: categorical}
        measures:
          - {name: category_amount, agg: sum, expr: total_spent}

    metrics:
      - name: grocery_spend_eur
        label: Grocery spend (EUR)
        type: simple
        type_params: {measure: basket_amount}
        meta:
          owner: alvaro
          grain_min: day
          description: Total invoice value, VAT included.
          excludes: Mercadona e-receipts only. Cash purchases are absent.

      - name: category_spend_eur
        label: Spend by category (EUR)
        type: simple
        type_params: {measure: category_amount}
        meta:
          owner: alvaro
          grain_min: day
          description: Line spend by LLM-assigned category.
          excludes: Uncategorised lines land in OTHER rather than being dropped.

      - name: shopping_trips
        label: Shopping trips
        type: simple
        type_params: {measure: basket_count}
        meta:
          owner: alvaro
          grain_min: day
          description: Number of invoices.
          excludes: One receipt is one trip.

      - name: avg_basket_eur
        label: Average basket (EUR)
        type: ratio
        type_params:
          numerator: basket_amount
          denominator: basket_count
        meta:
          owner: alvaro
          grain_min: week
          description: Mean invoice total across trips.
          excludes: Unweighted mean over trips, not over items.

      - name: blended_unit_price_eur
        label: Blended unit price (EUR)
        type: ratio
        type_params:
          numerator: line_amount
          denominator: line_quantity
        meta:
          owner: alvaro
          grain_min: week
          description: Spend divided by quantity.
          excludes: EUR/kg for weighted lines and EUR/unit otherwise. Not a headline number.
    """
)

_COLUMNS = {
    "invoices": [
        "invoice_number", "invoice_date", "total_amount", "supermarket", "payment_method",
    ],
    "invoice_items": ["invoice_number", "invoice_date", "total_amount", "quantity", "description_clean"],
    "category_spending": ["invoice_date", "category", "total_spent", "supermarket"],
}

_CATALOG = {"invoices": "silver", "invoice_items": "silver", "category_spending": "gold"}


@pytest.fixture
def manifest() -> dict:
    return {
        "nodes": {
            f"model.bodega.{name}": {
                "name": name,
                "resource_type": "model",
                "database": _CATALOG[name],
                "schema": "bodega",
                "alias": name,
                "columns": {column: {"name": column} for column in columns},
            }
            for name, columns in _COLUMNS.items()
        }
    }


@pytest.fixture
def registry_yaml(tmp_path):
    path = tmp_path / "bodega.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def registry(registry_yaml, manifest):
    loaded = registry_module.load(registry_yaml, version="testsha", manifest=manifest)
    registry_module.bind_tables(loaded, manifest)
    return loaded


@pytest.fixture
def samples() -> dict[str, list[str]]:
    return {"supermarket": ["MERCADONA"], "category": ["DAIRY_EGGS", "BEVERAGES", "OTHER"]}


@pytest.fixture
def wired(registry, monkeypatch):
    """Point the tool modules at the test registry, bypassing the file/env path."""
    from semantic import settings

    monkeypatch.setattr(settings, "registry", lambda: registry)
    monkeypatch.setattr(
        settings,
        "samples",
        lambda model: {
            "supermarket": {"cardinality": 1, "values": ["MERCADONA"]},
            "category": {"cardinality": 3, "values": ["DAIRY_EGGS", "BEVERAGES", "OTHER"]},
        }
        if model in ("invoices", "category_spending")
        else {},
    )
    monkeypatch.setattr(
        settings,
        "sample_values",
        lambda model: {"supermarket": ["MERCADONA"], "category": ["DAIRY_EGGS", "BEVERAGES", "OTHER"]}
        if model in ("invoices", "category_spending")
        else {},
    )
    return registry

"""The traffic and readiness expressions, and that nothing here names a vendor.

Each assertion is a mistake made while building the tools against a live
cluster, not a hypothetical. The label trap in particular is invisible in a
prompt review: grouping by the wrong label returns a plausible single row.
"""

from __future__ import annotations

import re

import pytest
from homelab_facts.tools import traffic
from homelab_facts.tools.traffic import (
    build_error_expression,
    build_rate_expression,
    build_shortfall_expression,
    clean_service,
)

TRAEFIK = "traefik_service_requests_total"
NGINX = "nginx_ingress_controller_requests"


class TestRateWindow:
    """A counter read bare is a lifetime total, not a rate."""

    def test_rate_wraps_the_counter(self):
        assert f"rate({TRAEFIK}[24h])" in build_rate_expression(TRAEFIK, "exported_service", "24h")

    def test_the_window_is_never_left_outside_the_function(self):
        # A 4B model handed `rate(m[1h])` has been observed sending `m[1h]` and
        # labelling the answer a rate; the server must never emit that shape.
        expression = build_rate_expression(TRAEFIK, "exported_service", "1h")
        assert re.findall(r"\w+\[1h\]", expression) == [f"{TRAEFIK}[1h]"]
        assert re.search(rf"rate\({TRAEFIK}\[1h\]\)", expression)


class TestServiceLabel:
    """Prometheus relabels an exporter's `service` to `exported_service` when a
    ServiceMonitor already owns the name. Grouping on the wrong one collapses
    the whole fleet into one row, with no error anywhere."""

    def test_rate_groups_by_the_configured_label(self):
        assert "sum by (exported_service)" in build_rate_expression(TRAEFIK, "exported_service", "24h")

    def test_a_different_label_is_honoured(self):
        assert "sum by (service)" in build_rate_expression(NGINX, "service", "24h")

    def test_error_share_groups_by_the_same_label(self):
        expression = build_error_expression(TRAEFIK, "exported_service", "code", "24h")
        assert expression.count("sum by (exported_service)") == 2


class TestErrorShare:
    """The 5xx column is a share of the service's own traffic, not of the fleet."""

    def test_error_expression_divides_by_the_same_service(self):
        expression = build_error_expression(TRAEFIK, "exported_service", "code", "24h")
        assert 'code=~"5.."' in expression
        assert expression.startswith("100 * ")
        assert " / " in expression

    def test_the_status_label_is_configurable(self):
        # nginx calls it `status`, Traefik `code`.
        assert 'status=~"5.."' in build_error_expression(NGINX, "service", "status", "24h")


class TestRequiredSettings:
    """A guessed metric matches no series, and no series renders as a fleet
    nobody uses - a wrong reading in the shape of an empty one. Same reasoning
    as PROMETHEUS_URL having no default."""

    def test_metric_is_required(self, monkeypatch):
        monkeypatch.delenv("INGRESS_REQUESTS_METRIC", raising=False)
        with pytest.raises(ValueError, match="INGRESS_REQUESTS_METRIC"):
            traffic.requests_metric()

    def test_label_is_required(self, monkeypatch):
        monkeypatch.delenv("INGRESS_SERVICE_LABEL", raising=False)
        with pytest.raises(ValueError, match="INGRESS_SERVICE_LABEL"):
            traffic.service_label()

    def test_a_missing_setting_is_reported_not_raised(self, monkeypatch):
        # The tool answers with a readable ERROR line; it must not take the
        # server down, and must not report an empty fleet either.
        monkeypatch.delenv("INGRESS_REQUESTS_METRIC", raising=False)
        result = traffic.top_services()
        assert result.startswith("ERROR:")
        assert "INGRESS_REQUESTS_METRIC" in result


class TestServiceNames:
    """Traefik router names carry a config hash that changes when the route is
    edited - kept, the same service reads as new on every run."""

    def test_the_configured_pattern_trims_the_name(self):
        pattern = re.compile(r"-[0-9a-f]{16,}@[a-z]+$")
        assert (
            clean_service("data-core-superset-c5d8967d86cf50ee1c5a@kubernetescrd", pattern)
            == "data-core-superset"
        )

    def test_no_pattern_means_no_trimming(self):
        assert clean_service("anything@at-all", None) == "anything@at-all"

    def test_trimming_never_empties_a_name(self):
        # A pattern matching the whole string would otherwise produce a blank row.
        assert clean_service("abc", re.compile(r".*")) == "abc"

    def test_an_invalid_pattern_is_ignored_not_fatal(self, monkeypatch):
        monkeypatch.setenv("INGRESS_SERVICE_STRIP_PATTERN", "(unclosed")
        assert traffic._strip_pattern() is None


class TestShortfall:
    """kube-state-metrics gives ready and desired different label sets, so an
    unqualified comparison silently matches nothing and every workload reads
    healthy."""

    def test_shortfall_carries_the_join(self):
        expression = build_shortfall_expression(
            "kube_deployment_status_replicas_available",
            "kube_deployment_spec_replicas",
            "on(namespace, deployment)",
        )
        assert "on(namespace, deployment)" in expression
        assert " < " in expression

    def test_direction_flags_too_few_not_too_many(self):
        # ready < desired. Reversed, it reports over-provisioned workloads and
        # can never report an outage - the volume-fill inversion in a new place.
        expression = build_shortfall_expression("ready", "desired", "on(namespace)")
        assert expression.index("ready") < expression.index("desired")
        assert ">" not in expression


class TestNoVendorInTheCode:
    """The one property this repository exists to keep: the image is generic."""

    def test_the_module_names_no_ingress_controller(self):
        from pathlib import Path

        source = Path(traffic.__file__).read_text().lower()
        # Named in prose as examples, but never as a value the code falls back to.
        for vendor in ("traefik_service_requests_total", "kubernetescrd", "exported_service"):
            assert f'"{vendor}"' not in source, vendor

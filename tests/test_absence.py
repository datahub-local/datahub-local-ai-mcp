"""Absence must be expressible, and it must not absorb bugs.

`endpoint-warden` owed seven columns per node against a 404ing Prometheus, so it
filled the disk column from the only tool that answered - relabelling
`k8s_nodes_top` memory as disk and calling a 5%-full control-plane disk "79% disk
fill (CRITICAL)". The standing "never report a number you did not retrieve" rule
lost to a format that demanded a value.

`unavailable` was introduced to fix that, and then silently absorbed a broken
join: four rows read `unavailable` when every figure was available. So absence
needs a *definition*, which is why there are two words for it.
"""

from __future__ import annotations

from mcp_runner.prometheus import UNAVAILABLE, Reading
from mcp_runner.render import age, days_until, number


class TestUnavailableIsAValue:
    def test_a_missing_number_renders_as_the_literal(self):
        assert number(None) == UNAVAILABLE

    def test_unavailable_is_never_zero(self):
        assert number(None) != "0.0"
        assert number(0.0) == "0.0"
        assert number(0.0) != UNAVAILABLE

    def test_a_real_zero_survives(self):
        # Zero pending security updates is a reading, not an absence.
        assert number(0.0, 0) == "0"


class TestReadingSeparatesFailureFromEmptiness:
    """The distinction that stops `unavailable` swallowing a broken join."""

    def test_a_failed_query_is_not_ok(self):
        assert Reading(values={}, ok=False).ok is False

    def test_an_empty_but_successful_query_is_ok(self):
        # "No volume matched" is a real answer; "the query failed" is not.
        assert Reading(values={}, ok=True).ok is True

    def test_coverage_reports_which_keys_answered(self):
        reading = Reading(values={"a": 1.0}, ok=True)
        assert reading.covered() == {"a"}


class TestNodeFleetAbsence:
    def test_a_failed_query_makes_the_column_unavailable_for_every_node(
        self, monkeypatch, fake_prometheus, sample_series, kube_with
    ):
        from homelab_facts import settings
        from homelab_facts.tools import nodes

        # Everything answers except disk, which fails.
        answers = {
            expression: [sample_series(1.0, nodename="n1")] for expression in nodes._QUERIES.values()
        }
        answers["node_uname_info"] = [
            {"metric": {"nodename": "n1", "release": "6.1.0-x", "machine": "aarch64"}, "value": [0, "1"]}
        ]
        fake = fake_prometheus(answers=answers, fail={nodes._QUERIES["disk_pct"]})
        monkeypatch.setattr(settings, "prometheus", lambda: fake)
        monkeypatch.setattr(settings, "kube", lambda: kube_with())

        out = nodes.node_fleet()
        assert UNAVAILABLE in out
        assert "Queries that FAILED" in out

    def test_a_node_with_no_sensor_is_na_not_unavailable(
        self, monkeypatch, fake_prometheus, sample_series, kube_with
    ):
        from homelab_facts import settings
        from homelab_facts.tools import nodes

        answers = {
            expression: [sample_series(1.0, nodename="n1")] for expression in nodes._QUERIES.values()
        }
        answers["node_uname_info"] = [
            {"metric": {"nodename": "n1", "release": "6.1.0-x", "machine": "aarch64"}, "value": [0, "1"]}
        ]
        # The SMART capability probe answers 0: the device cannot report health.
        for expression, probe, _agg in nodes._SCOPED.values():
            answers[expression] = []
            answers[probe] = [sample_series(0.0, nodename="n1")]
        fake = fake_prometheus(answers=answers)
        monkeypatch.setattr(settings, "prometheus", lambda: fake)
        monkeypatch.setattr(settings, "kube", lambda: kube_with())

        out = nodes.node_fleet()
        # n/a, because a node whose drives report smart_available=0 is hardware,
        # not a fault, and must not be filed as a finding.
        assert "n/a" in out

    def test_a_node_in_kubernetes_but_absent_from_metrics_is_named(
        self, monkeypatch, fake_prometheus, kube_with
    ):
        from homelab_facts import settings
        from homelab_facts.tools import nodes

        fake = fake_prometheus(answers={})
        monkeypatch.setattr(settings, "prometheus", lambda: fake)
        monkeypatch.setattr(settings, "kube", lambda: kube_with(["ghost-node"]))

        out = nodes.node_fleet()
        # Scattered absences almost always mean the join, and the tool says so.
        assert "ghost-node" in out
        assert "absent from every metric" in out


class TestNodeUpdateFindings:
    @staticmethod
    def _scoped():
        return {
            key: (Reading(values={}, ok=True), set())
            for key in ("smart_healthy", "smart_temp_c", "edac_corr", "edac_uncorr")
        }

    def test_pending_security_and_total_updates_are_reported(self):
        from homelab_facts.tools import nodes

        readings = {
            key: Reading(values={"n1": 0.0}, ok=True) for key in nodes._QUERIES
        }
        readings["apt_security"] = Reading(values={"n1": 2.0}, ok=True)
        readings["apt_total"] = Reading(values={"n1": 5.0}, ok=True)

        notes = nodes._notes(
            readings,
            self._scoped(),
            ["n1"],
            {"security_updates_warn": 1},
            "",
        )

        assert any("2 security update(s) pending" in note for note in notes)
        assert any("5 total package update(s) pending" in note for note in notes)

    def test_zero_pending_updates_are_not_findings(self):
        from homelab_facts.tools import nodes

        readings = {
            key: Reading(values={"n1": 0.0}, ok=True) for key in nodes._QUERIES
        }
        readings["systemd_ok"] = Reading(values={"n1": 1.0}, ok=True)

        notes = nodes._notes(
            readings,
            self._scoped(),
            ["n1"],
            {"security_updates_warn": 1},
            "",
        )

        assert notes == ["Nothing outside a threshold."]

    def test_critical_temperature_uses_the_critical_threshold(self):
        from homelab_facts.tools import nodes

        readings = {
            key: Reading(values={"n1": 0.0}, ok=True) for key in nodes._QUERIES
        }
        readings["systemd_ok"] = Reading(values={"n1": 1.0}, ok=True)
        readings["temp_c"] = Reading(values={"n1": 90.0}, ok=True)

        notes = nodes._notes(
            readings,
            self._scoped(),
            ["n1"],
            {
                "temperature_warn_celsius": 75,
                "temperature_critical_celsius": 85,
            },
            "",
        )

        assert any("90.0C (CRITICAL, threshold 85C)" in note for note in notes)


class TestAgesAreToolResults:
    """Nothing in an agent run injects a clock, so the server does the subtraction."""

    def test_a_future_date_reads_as_in_n_days(self):
        import datetime

        now = datetime.datetime(2026, 8, 25, tzinfo=datetime.UTC)
        assert age("2026-09-08T00:00:00Z", now=now) == "in 14.0d"

    def test_a_past_date_reads_as_n_days_ago(self):
        import datetime

        now = datetime.datetime(2026, 8, 25, tzinfo=datetime.UTC)
        assert age("2026-08-11T00:00:00Z", now=now) == "14.0d ago"

    def test_the_direction_is_spelled_out_not_signed(self):
        # "expires in 14 days" and "expired 14 days ago" are opposite findings,
        # and a leading minus is the easiest character in a table to drop.
        import datetime

        now = datetime.datetime(2026, 8, 25, tzinfo=datetime.UTC)
        future = age("2026-09-08T00:00:00Z", now=now)
        past = age("2026-08-11T00:00:00Z", now=now)
        assert not future.startswith("-")
        assert "ago" in past and "ago" not in future

    def test_an_expired_certificate_has_negative_days_remaining(self):
        import datetime

        now = datetime.datetime(2026, 8, 25, tzinfo=datetime.UTC)
        assert days_until("2026-08-01T00:00:00Z", now=now) < 0

    def test_an_unparseable_date_is_unavailable_not_now(self):
        assert age("not-a-date") == UNAVAILABLE
        assert days_until(None) is None

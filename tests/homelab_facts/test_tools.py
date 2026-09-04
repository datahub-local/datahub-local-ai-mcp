"""The facts tools end to end, against a fake Prometheus.

These are the tests that would have caught the reports that reached Slack.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets its own snapshot store, so diffs do not leak between tests."""
    from homelab_facts import settings

    from mcp_runner.state import Snapshots

    store = Snapshots(str(tmp_path / "state"))
    monkeypatch.setattr(settings, "snapshots", lambda: store)
    return store


class TestVolumeFill:
    """The inversion that called a 2%-used volume "97.9% full" for days."""

    def _run(self, monkeypatch, fake_prometheus, sample_series, samples):
        from homelab_facts import settings
        from homelab_facts.tools import volumes

        expression = volumes.build_expression(["longhorn", "longhorn-no-replica"])
        fake = fake_prometheus(answers={expression: samples})
        monkeypatch.setattr(settings, "prometheus", lambda: fake)
        return volumes.volume_fill(), fake

    def test_an_almost_empty_volume_is_reported_as_nearly_empty(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        out, _ = self._run(
            monkeypatch,
            fake_prometheus,
            sample_series,
            [sample_series(2.1, namespace="data", persistentvolumeclaim="pvc-a")],
        )
        assert "2.1%" in out
        # The exact wrong output this replaces: "97.9% full, write operations failing".
        assert "97.9" not in out
        assert "CRITICAL" not in out

    def test_a_nearly_full_volume_is_critical(self, monkeypatch, fake_prometheus, sample_series):
        out, _ = self._run(
            monkeypatch,
            fake_prometheus,
            sample_series,
            [sample_series(93.0, namespace="data", persistentvolumeclaim="pvc-b")],
        )
        assert "93.0%" in out
        assert "CRITICAL" in out

    def test_the_query_sent_carries_the_inversion_and_the_join(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        _, fake = self._run(monkeypatch, fake_prometheus, sample_series, [])
        assert len(fake.seen) == 1
        sent = fake.seen[0]
        assert sent.startswith("100 * (1 - ")
        assert "group_left(storageclass)" in sent

    def test_an_empty_answer_is_not_reported_as_healthy(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        out, _ = self._run(monkeypatch, fake_prometheus, sample_series, [])
        assert "unavailable" in out
        assert "No volumes matched" in out

    def test_the_change_column_is_computed_between_runs(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts import settings
        from homelab_facts.tools import volumes

        expression = volumes.build_expression(["longhorn", "longhorn-no-replica"])

        first = fake_prometheus(
            answers={expression: [sample_series(40.0, namespace="d", persistentvolumeclaim="p")]}
        )
        monkeypatch.setattr(settings, "prometheus", lambda: first)
        out_one = volumes.volume_fill()
        assert "first" in out_one

        second = fake_prometheus(
            answers={expression: [sample_series(55.0, namespace="d", persistentvolumeclaim="p")]}
        )
        monkeypatch.setattr(settings, "prometheus", lambda: second)
        out_two = volumes.volume_fill()
        # 15 points of growth, measured rather than remembered.
        assert "15.0pp" in out_two

    def test_the_result_stays_inside_its_budget_with_many_claims(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import volumes

        many = [
            sample_series(50.0, namespace="data", persistentvolumeclaim=f"pvc-{index:03d}-long-name")
            for index in range(400)
        ]
        out, _ = self._run(monkeypatch, fake_prometheus, sample_series, many)
        assert len(out.encode()) <= volumes.BUDGET
        assert "TRUNCATED" in out


class TestAlertsSnapshot:
    """The chronic/real distinction, and the diff."""

    def _run(self, monkeypatch, fake_prometheus, samples):
        from homelab_facts import settings
        from homelab_facts.tools import alerts

        fake = fake_prometheus(answers={alerts._EXPRESSION: samples})
        monkeypatch.setattr(settings, "prometheus", lambda: fake)
        return alerts.alerts_snapshot()

    def test_a_chronic_alert_is_classified_not_dropped(self, monkeypatch, fake_prometheus):
        out = self._run(
            monkeypatch,
            fake_prometheus,
            [{"metric": {"alertname": "Watchdog", "severity": "none"}, "value": [0, "1"]}],
        )
        assert "Watchdog" in out
        assert "chronic" in out

    def test_a_real_permanent_alert_is_never_folded_into_chronic(
        self, monkeypatch, fake_prometheus
    ):
        # NodeClockNotSynchronising fires forever AND is genuinely broken. A
        # chronic list that absorbed it would hide a real fault indefinitely.
        out = self._run(
            monkeypatch,
            fake_prometheus,
            [
                {
                    "metric": {
                        "alertname": "NodeClockNotSynchronising",
                        "severity": "warning",
                        "instance": "node-1",
                    },
                    "value": [0, "1"],
                }
            ],
        )
        assert "REAL-chronic" in out
        assert "New since last run (1)" in out

    def test_an_unknown_alert_is_new_and_unclassified(self, monkeypatch, fake_prometheus):
        out = self._run(
            monkeypatch,
            fake_prometheus,
            [{"metric": {"alertname": "SomethingBrandNew", "pod": "p"}, "value": [0, "1"]}],
        )
        assert "SomethingBrandNew" in out
        assert "New since last run (1)" in out

    def test_nothing_firing_is_stated_plainly(self, monkeypatch, fake_prometheus):
        out = self._run(monkeypatch, fake_prometheus, [])
        assert "Nothing is firing." in out

    def test_a_failed_query_is_not_no_alerts(self, monkeypatch, fake_prometheus):
        from homelab_facts import settings
        from homelab_facts.tools import alerts

        fake = fake_prometheus(answers={}, fail={alerts._EXPRESSION})
        monkeypatch.setattr(settings, "prometheus", lambda: fake)
        out = alerts.alerts_snapshot()
        assert out.startswith("ERROR")
        assert "not 'no alerts firing'" in out

    def test_the_second_run_moves_alerts_from_new_to_still_firing(
        self, monkeypatch, fake_prometheus
    ):
        samples = [{"metric": {"alertname": "Foo", "pod": "p"}, "value": [0, "1"]}]
        first = self._run(monkeypatch, fake_prometheus, samples)
        assert "New since last run (1)" in first
        second = self._run(monkeypatch, fake_prometheus, samples)
        assert "New since last run (0)" in second
        assert "Still firing (1)" in second

    def test_a_disappeared_alert_is_reported_resolved(self, monkeypatch, fake_prometheus):
        self._run(
            monkeypatch,
            fake_prometheus,
            [{"metric": {"alertname": "Foo", "pod": "p"}, "value": [0, "1"]}],
        )
        out = self._run(monkeypatch, fake_prometheus, [])
        assert "Resolved since last run (1)" in out
        assert "Foo" in out

    def test_many_instances_of_one_alert_are_compressed(self, monkeypatch, fake_prometheus):
        out = self._run(
            monkeypatch,
            fake_prometheus,
            [
                {"metric": {"alertname": "CPUThrottlingHigh", "pod": f"pod-{index}"}, "value": [0, "1"]}
                for index in range(40)
            ],
        )
        from homelab_facts.tools import alerts

        assert len(out.encode()) <= alerts.BUDGET
        assert "x40" in out


class TestArgocdDrift:
    def _run(self, monkeypatch, apps):
        from homelab_facts import settings
        from homelab_facts.tools import gitops

        class Kube:
            def list(self, *args, **kwargs):
                return apps

        monkeypatch.setattr(settings, "kube", lambda: Kube())
        return gitops.argocd_drift()

    def _app(self, name, sync="Synced", health="Healthy"):
        return {
            "metadata": {"name": name},
            "status": {"sync": {"status": sync}, "health": {"status": health}},
        }

    def test_a_healthy_fleet_reports_no_drift(self, monkeypatch):
        out = self._run(monkeypatch, [self._app("a"), self._app("b")])
        assert "0 not Synced+Healthy" in out

    def test_drift_is_counted_across_consecutive_runs(self, monkeypatch):
        apps = [self._app("a", sync="OutOfSync")]
        first = self._run(monkeypatch, apps)
        assert "1 not Synced+Healthy" in first
        second = self._run(monkeypatch, apps)
        # Escalation without the model remembering anything.
        lines = [line for line in second.splitlines() if line.startswith("a ")]
        assert lines and lines[0].split()[-1] == "2"

    def test_no_applications_is_unavailable_not_synced(self, monkeypatch):
        out = self._run(monkeypatch, [])
        assert out.startswith("unavailable")
        assert "not a synced one" in out

    def test_the_resource_tree_is_never_returned(self, monkeypatch):
        # argocd_get_application_resource_tree was ~16 KB on its own and ended a
        # run with no report at all.
        from homelab_facts.tools import gitops

        out = self._run(monkeypatch, [self._app("a")])
        assert len(out.encode()) <= gitops.BUDGET
        assert "not the resource tree" in out


class TestKernelSection:
    """The probe of 2026-08-25: right reading, inverted conclusion.

    `node_fleet` printed `deb13-amd64/x86_64: DRIFT within class - amd-1 behind`
    and closed the section with a paragraph ending "not drift and can never be
    actioned" - a statement about a difference *across* classes. The 4B model
    attached that clause to the DRIFT line above it and wrote "kernel drift on
    amd-1 is within its hardware class and cannot be actioned", dismissing the
    one genuinely actionable finding in the fleet. Adjacent prose became the
    verdict, the same way `increase(m[1h])` lost its wrapper.
    """

    def _lines(self):
        from homelab_facts.tools import nodes

        from tests.test_fleet import KERNELS, MACHINES

        return nodes._kernel_drift(KERNELS, MACHINES, "datahublocal-")

    def _drift_line(self):
        return next(line for line in self._lines() if "DRIFT" in line)

    def test_the_scope_note_leads_the_section_and_never_trails_a_finding(self):
        # Position is the fix: a model summarising a section takes the last line
        # as the conclusion, so the caveat cannot be last.
        lines = self._lines()
        assert lines[0].startswith("A class is one kernel tree")
        assert "DRIFT" not in lines[0]
        assert "DRIFT" in lines[-1] or "drift" in lines[-1]

    def test_no_line_in_the_section_says_actioned(self):
        # The exact phrase the model borrowed. Nothing here may carry it.
        assert not any("action" in line for line in self._lines())

    def test_the_drift_line_states_its_own_verdict(self):
        # Every line carries its verdict, so none has to borrow one.
        line = self._drift_line()
        assert "can converge" in line
        assert "real finding" in line

    def test_the_drift_line_names_the_node_and_both_versions(self):
        line = self._drift_line()
        assert "amd-1 behind" in line
        assert "amd-1=6.12.96" in line and "amd-2=6.12.101" in line

    def test_a_class_of_one_is_still_stated_as_never_drift(self):
        # orpi-0, the finding that could never clear.
        line = next(line for line in self._lines() if "edge-rockchip64" in line)
        assert "never drift" in line
        assert "DRIFT" not in line

    def test_the_cross_class_rule_says_not_compared_rather_than_not_actionable(self):
        assert "are not compared here at all" in self._lines()[0]

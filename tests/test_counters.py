"""Counters vs gauges, and the archiver verdict that paged CRITICAL for days.

`cnpg_pg_stat_archiver_failed_count` was called "the most important number you
look at" and never described as a counter. A lifetime total of 2 - two failures
5.4 days old, against a successful archive 95 seconds old and
`increase(...[24h]) = 0` - paged CRITICAL "recovery is silently broken" on every
run, twice into Slack.
"""

from __future__ import annotations

from homelab_facts.tools.databases import (
    CUMULATIVE_COUNTERS,
    GAUGES_LOOKING_LIKE_COUNTERS,
    _archiver_verdict,
)


class TestSuffixIsNoGuide:
    """The trap that makes a suffix-based rule wrong in both directions.

    Verified against Prometheus's metadata API on 2026-08-25:
    `cnpg_backends_total` and `cnpg_backends_waiting_total` are **gauges** despite
    `_total`, while `cnpg_pg_stat_archiver_failed_count` is a **counter** despite
    `_count`. A rule keyed on the suffix gets both wrong, which is why the set is
    explicit and sourced from the metadata API.
    """

    def test_a_total_suffix_does_not_make_it_a_counter(self):
        for metric in GAUGES_LOOKING_LIKE_COUNTERS:
            assert metric.endswith("_total")
            assert metric not in CUMULATIVE_COUNTERS

    def test_a_count_suffix_does_not_make_it_a_gauge(self):
        assert "cnpg_pg_stat_archiver_failed_count" in CUMULATIVE_COUNTERS

    def test_the_two_sets_never_overlap(self):
        assert not (CUMULATIVE_COUNTERS & GAUGES_LOOKING_LIKE_COUNTERS)

    def test_a_suffix_rule_would_fail_this_cluster(self):
        # Demonstrates why the explicit set exists rather than asserting the set.
        def guess_by_suffix(metric: str) -> bool:
            return metric.endswith(("_total", "_count"))

        wrong = [m for m in GAUGES_LOOKING_LIKE_COUNTERS if guess_by_suffix(m)]
        assert wrong, "a suffix rule must be shown to be wrong, or this test is vacuous"


class TestArchiverVerdict:
    """The reading itself: an increase is a state, a lifetime total is not."""

    def test_zero_increase_with_a_recent_success_is_healthy(self):
        assert _archiver_verdict(0.0, 95.0, 1, 3600) == "Healthy."

    def test_the_exact_case_that_paged_critical_reads_healthy(self):
        # A lifetime total of 2 cannot reach this function at all: the caller only
        # ever passes an increase. Two failures 5.4 days ago with a success 95
        # seconds ago is a healthy archiver.
        assert _archiver_verdict(0.0, 95.0, 1, 3600) == "Healthy."

    def test_a_nonzero_increase_is_critical(self):
        assert "CRITICAL" in _archiver_verdict(3.0, 95.0, 1, 3600)

    def test_a_stale_last_success_warns_even_with_no_failures(self):
        # The other failure mode: nothing is failing because nothing is trying.
        assert "WARN" in _archiver_verdict(0.0, 7200.0, 1, 3600)

    def test_missing_metrics_are_unavailable_not_healthy(self):
        assert _archiver_verdict(None, None, 1, 3600) == "unavailable."

    def test_postgres_health_never_queries_the_bare_counter(self):
        # The wrapper is the guard: if the bare metric were sent, the model would
        # be shown the lifetime total it previously misread.
        import inspect

        from homelab_facts.tools import databases

        source = inspect.getsource(databases.postgres_health)
        assert "increase_(\"cnpg_pg_stat_archiver_failed_count\"" in source

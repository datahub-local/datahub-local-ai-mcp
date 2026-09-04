"""Garage, Redpanda and Prometheus's own TSDB, against a fake Prometheus.

Four readings here are wrong when assembled by hand, and each has a test rather
than a paragraph in a prompt: Garage's informative series carry no `garage_`
prefix, its three nodes describe one shared filesystem, Redpanda's cluster
scalars come from the controller leader alone, and a young TSDB is not a lossy
one.
"""

from __future__ import annotations

import re
from typing import ClassVar

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    from homelab_facts import settings

    from mcp_runner.garage import Garage
    from mcp_runner.state import Snapshots

    monkeypatch.setattr(settings, "snapshots", lambda: Snapshots(str(tmp_path / "state")))
    # No token is the default state of this server, so it is the default here.
    monkeypatch.setattr(settings, "garage", lambda: Garage(url="http://garage.test", token=None))


class FakeGarage:
    """The two admin GETs, answered from a dict. Records nothing else, because
    there is nothing else to call."""

    def __init__(self, buckets=None, info=None, error=None):
        self._buckets = buckets or []
        self._info = info or {}
        self._error = error

    def configured(self):
        return True

    def list_buckets(self):
        if self._error:
            raise self._error
        return self._buckets

    def bucket_info(self, bucket_id):
        from mcp_runner.garage import GarageError

        if bucket_id not in self._info:
            raise GarageError("no such bucket")
        return self._info[bucket_id]


def _wire(monkeypatch, fake_prometheus, answers, fail=None):
    from homelab_facts import settings

    fake = fake_prometheus(answers=answers, fail=fail)
    monkeypatch.setattr(settings, "prometheus", lambda: fake)
    return fake


def _garage_answers(sample, *, metadata, data_avail, data_total, data_used):
    """The subset of Garage's readings a test needs, keyed by the exact
    expression the tool sends - so an expression that changes shape fails here
    rather than silently answering nothing."""
    from homelab_facts.tools import stores

    pods = sorted(metadata)
    answers = {
        f"cluster_healthy{stores.GARAGE}": [sample(1, pod=pod) for pod in pods],
        f"cluster_available{stores.GARAGE}": [sample(1, pod=pod) for pod in pods],
        f"cluster_connected_nodes{stores.GARAGE}": [sample(3, pod=pod) for pod in pods],
        f"cluster_known_nodes{stores.GARAGE}": [sample(3, pod=pod) for pod in pods],
        f"cluster_storage_nodes_ok{stores.GARAGE}": [sample(3, pod=pod) for pod in pods],
        f"cluster_storage_nodes{stores.GARAGE}": [sample(3, pod=pod) for pod in pods],
        f"cluster_partitions_quorum{stores.GARAGE}": [sample(256, pod=pod) for pod in pods],
        f"cluster_partitions{stores.GARAGE}": [sample(256, pod=pod) for pod in pods],
        f"cluster_partitions_all_ok{stores.GARAGE}": [sample(256, pod=pod) for pod in pods],
        stores._garage_disk("metadata"): [sample(v, pod=k) for k, v in metadata.items()],
        stores._garage_disk("data"): [sample(v, pod=k) for k, v in data_used.items()],
        f'garage_local_disk_avail{stores.GARAGE[:-1]},volume="data"}}': [
            sample(v, pod=k) for k, v in data_avail.items()
        ],
        f'garage_local_disk_total{stores.GARAGE[:-1]},volume="data"}}': [
            sample(v, pod=k) for k, v in data_total.items()
        ],
        f"max(garage_replication_factor{stores.GARAGE})": [sample(1)],
    }
    return answers


def _one_node(sample):
    """Enough of a Garage to get past the tool's early return.

    An answer with nothing in it is reported as `unavailable` and the tool stops
    there on purpose, so a test asserting on a later section has to give it a
    cluster to describe.
    """
    return _garage_answers(
        sample,
        metadata={"g-0": 4.0},
        data_avail={"g-0": 10.0},
        data_total={"g-0": 100.0},
        data_used={"g-0": 90.0},
    )


class TestGarageIsNotFoundByItsPrefix:
    """Only four of Garage's metrics carry a `garage_` prefix.

    The cluster, table, block and S3 API series are bare `cluster_healthy`,
    `table_size`, `block_resync_*` and `api_s3_*`. A reader looking for
    `garage_*` finds four metrics and concludes the store is barely
    instrumented; a bare name matches whatever else publishes it. Both are fixed
    by scoping every query to the job.
    """

    def test_every_query_the_tool_sends_is_job_scoped(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        fake = _wire(monkeypatch, fake_prometheus, _one_node(sample_series))
        stores.object_store_health()
        assert fake.seen, "the tool sent no query at all"
        for sent in fake.seen:
            assert 'job=~".*garage.*"' in sent, sent

    def test_the_bare_names_are_the_ones_actually_queried(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        # If these ever grew a `garage_` prefix upstream, every reading below
        # would go quiet while the tool still rendered a full report.
        from homelab_facts.tools import stores

        fake = _wire(monkeypatch, fake_prometheus, _one_node(sample_series))
        stores.object_store_health()
        joined = " ".join(fake.seen)
        for bare in ("cluster_healthy", "cluster_partitions_all_ok", "block_resync_errored_blocks"):
            assert bare in joined
            assert f"garage_{bare}" not in joined


class TestOneSharedFilesystem:
    """The three data volumes are one nfs share seen three times.

    Same class of error as the nfs PVCs in `volume_fill`: a per-node percentage
    is the share's fill repeated once per node, and it invites "garage-2 is
    filling up" about a disk no single node can fill.
    """

    def test_identical_readings_are_reported_once(self, monkeypatch, fake_prometheus, sample_series):
        from homelab_facts.tools import stores

        pods = ["g-0", "g-1", "g-2"]
        _wire(
            monkeypatch,
            fake_prometheus,
            _garage_answers(
                sample_series,
                metadata=dict.fromkeys(pods, 4.0),
                data_avail=dict.fromkeys(pods, 1_906_909_904_896.0),
                data_total=dict.fromkeys(pods, 1_926_796_148_736.0),
                data_used=dict.fromkeys(pods, 1.0),
            ),
        )
        out = stores.object_store_health()
        section = out.split("## Data filesystem")[1].split("##")[0]
        assert "one shared filesystem" in section
        # One figure, not one row per node.
        for pod in pods:
            assert pod not in section

    def test_differing_capacities_are_reported_per_node(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        # Derived, not configured: the day the nodes get their own disks the
        # tool reports three rows without an edit here.
        from homelab_facts.tools import stores

        _wire(
            monkeypatch,
            fake_prometheus,
            _garage_answers(
                sample_series,
                metadata={"g-0": 4.0, "g-1": 4.0},
                data_avail={"g-0": 10.0, "g-1": 20.0},
                data_total={"g-0": 100.0, "g-1": 200.0},
                data_used={"g-0": 90.0, "g-1": 90.0},
            ),
        )
        section = stores.object_store_health().split("## Data filesystem")[1].split("##")[0]
        assert "g-0" in section and "g-1" in section
        assert "one shared filesystem" not in section

    def test_a_single_reporter_is_never_called_shared(self):
        from homelab_facts.tools.stores import _shared_filesystem

        assert not _shared_filesystem({"g-0": 1.0}, {"g-0": 2.0})

    def test_nodes_scraped_moments_apart_still_count_as_one_share(self):
        # The live reading on 2026-08-31: identical 1.9 TB totals, free space
        # 1 MB apart because the three nodes sampled the share at slightly
        # different moments. Byte equality called that three separate disks.
        from homelab_facts.tools.stores import _shared_filesystem

        total = dict.fromkeys(("g-0", "g-1", "g-2"), 1_926_796_148_736.0)
        available = {
            "g-0": 1_906_886_836_224.0,
            "g-1": 1_906_886_836_224.0,
            "g-2": 1_906_887_884_800.0,
        }
        assert _shared_filesystem(available, total)

    def test_genuinely_different_free_space_is_not_one_share(self):
        from homelab_facts.tools.stores import _shared_filesystem

        total = dict.fromkeys(("g-0", "g-1"), 100.0)
        assert not _shared_filesystem({"g-0": 90.0, "g-1": 10.0}, total)


class TestGarageReadings:
    def test_the_metadata_percentage_is_used_and_not_free(self):
        from homelab_facts.tools import stores

        expression = stores._garage_disk("metadata")
        assert expression.startswith("100 * (1 - ")
        assert not re.match(r"^\s*garage_local_disk_avail\s*/", expression)

    def test_an_empty_answer_is_unavailable_and_not_healthy(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        _wire(monkeypatch, fake_prometheus, {})
        out = stores.object_store_health()
        assert out.startswith("unavailable")
        assert "## Cluster consensus" not in out

    def test_replication_factor_one_is_stated_once_and_never_critical(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        # A standing property of the configuration cannot be a new finding every
        # run - the same treatment Valkey's unset maxmemory gets.
        from homelab_facts.tools import stores

        pods = ["g-0"]
        _wire(
            monkeypatch,
            fake_prometheus,
            _garage_answers(
                sample_series,
                metadata=dict.fromkeys(pods, 4.0),
                data_avail=dict.fromkeys(pods, 10.0),
                data_total=dict.fromkeys(pods, 100.0),
                data_used=dict.fromkeys(pods, 90.0),
            ),
        )
        section = stores.object_store_health().split("## Durability")[1].split("##")[0]
        assert "Replication factor is 1" in section
        assert "CRITICAL" not in section

    def test_s3_errors_are_read_through_a_window_not_as_a_total(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        fake = _wire(monkeypatch, fake_prometheus, _one_node(sample_series))
        stores.object_store_health()
        error_queries = [sent for sent in fake.seen if "api_s3_error_counter" in sent]
        assert error_queries
        for sent in error_queries:
            assert "increase(" in sent

    def test_a_node_that_disagrees_shows_up_as_its_own_row(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        answers = _garage_answers(
            sample_series,
            metadata={"g-0": 4.0, "g-1": 4.0},
            data_avail={"g-0": 10.0, "g-1": 10.0},
            data_total={"g-0": 100.0, "g-1": 100.0},
            data_used={"g-0": 90.0, "g-1": 90.0},
        )
        answers[f"cluster_healthy{stores.GARAGE}"] = [
            sample_series(1, pod="g-0"),
            sample_series(0, pod="g-1"),
        ]
        _wire(monkeypatch, fake_prometheus, answers)
        out = stores.object_store_health()
        assert "NOT HEALTHY" in out


class TestLeaderOnlyClusterMetrics:
    """One Redpanda broker of three publishes the cluster-wide figures.

    Which one moves with leadership. Read per-pod that is two brokers with no
    data; aggregated it is the only correct reading, and "one reporter" is the
    normal state rather than a finding.
    """

    def _answers(self, sample, **overrides):
        from homelab_facts.tools import stores

        answers = {
            f"max(redpanda_cluster_brokers{stores.REDPANDA})": [sample(3, pod="rp-2")],
            f"max(redpanda_cluster_topics{stores.REDPANDA})": [sample(3, pod="rp-2")],
            f"max(redpanda_cluster_partitions{stores.REDPANDA})": [sample(18, pod="rp-2")],
            f"max(redpanda_cluster_unavailable_partitions{stores.REDPANDA})": [
                sample(0, pod="rp-2")
            ],
            f"sum(redpanda_kafka_under_replicated_replicas{stores.REDPANDA})": [sample(0)],
        }
        answers.update(overrides)
        return answers

    def test_one_reporting_broker_yields_the_whole_cluster_figure(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        answers = self._answers(sample_series)
        answers[
            stores.used_percent(
                "redpanda_storage_disk_free_bytes",
                "redpanda_storage_disk_total_bytes",
                stores.REDPANDA,
            )
        ] = [sample_series(20.0, pod=f"rp-{i}") for i in range(3)]
        _wire(monkeypatch, fake_prometheus, answers)
        out = stores.stream_health()
        assert "3" in out.split("## Broker")[0]
        assert "controller leader" in out
        assert "normal state" in out

    def test_the_cluster_figures_are_aggregated_never_per_pod(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        fake = _wire(monkeypatch, fake_prometheus, {})
        stores.stream_health()
        for sent in fake.seen:
            if "redpanda_cluster_" in sent:
                assert sent.startswith(("max(", "sum(")), sent


class TestRedpandaReadings:
    def test_the_disk_alert_enum_is_decoded_not_printed_as_a_number(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        # An enum rendered as a number is a number the model compares against a
        # threshold it invented.
        from homelab_facts.tools import stores

        expression = stores.used_percent(
            "redpanda_storage_disk_free_bytes",
            "redpanda_storage_disk_total_bytes",
            stores.REDPANDA,
        )
        _wire(
            monkeypatch,
            fake_prometheus,
            {
                expression: [sample_series(20.0, pod="rp-0")],
                f"redpanda_storage_disk_free_space_alert{stores.REDPANDA}": [
                    sample_series(2, pod="rp-0")
                ],
            },
        )
        out = stores.stream_health()
        assert "DEGRADED" in out

    def test_broker_disk_is_used_and_not_free(self):
        from homelab_facts.tools import stores

        expression = stores.used_percent(
            "redpanda_storage_disk_free_bytes",
            "redpanda_storage_disk_total_bytes",
            stores.REDPANDA,
        )
        assert expression.startswith("100 * (1 - ")

    def test_every_counter_is_wrapped_and_every_gauge_is_not(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        fake = _wire(monkeypatch, fake_prometheus, {})
        stores.stream_health()
        stores.object_store_health()
        stores.metrics_store_health()
        for sent in fake.seen:
            for counter in stores.STORE_COUNTERS:
                if counter in sent:
                    assert "increase(" in sent or "rate(" in sent, sent
            for gauge in stores.STORE_GAUGES:
                if gauge in sent:
                    assert "increase(" not in sent and "rate(" not in sent, sent

    def test_an_empty_answer_is_unavailable_and_not_healthy(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        _wire(monkeypatch, fake_prometheus, {})
        assert "unavailable" in stores.stream_health()

    def test_an_idle_cluster_is_not_reported_as_a_fault(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        answers = TestLeaderOnlyClusterMetrics()._answers(sample_series)
        answers[
            stores.used_percent(
                "redpanda_storage_disk_free_bytes",
                "redpanda_storage_disk_total_bytes",
                stores.REDPANDA,
            )
        ] = [sample_series(20.0, pod="rp-0")]
        for metric in (
            "redpanda_kafka_records_produced_total",
            "redpanda_kafka_records_fetched_total",
        ):
            answers[f"sum(increase({metric}{stores.REDPANDA}[1h]))"] = [sample_series(0)]
        _wire(monkeypatch, fake_prometheus, answers)
        out = stores.stream_health()
        assert "not by itself a fault" in out
        assert "CRITICAL" not in out


class TestRetentionVerdict:
    """A young TSDB is not a lossy one.

    Held history below the configured window is the normal state of a store that
    restarted recently. Judged against the configured window alone it is a
    finding on every run for a month - the permanent-finding shape that made
    orpi-0's kernel drift for days - so it is judged against the server's own
    uptime too.
    """

    _LIMITS: ClassVar[dict[str, float]] = {"retention_held_warn_ratio": 0.9}

    def test_a_full_window_is_at_depth(self):
        from homelab_facts.tools.stores import _retention_verdict

        assert _retention_verdict(30 * 86400, 29 * 86400, 60 * 86400, self._LIMITS) == (
            "At its configured depth."
        )

    def test_a_freshly_restarted_store_is_filling_not_losing(self):
        from homelab_facts.tools.stores import _retention_verdict

        # The exact live reading on 2026-08-31: 2.6h held of a configured 30d,
        # with the server up 2.6h.
        verdict = _retention_verdict(30 * 86400, 9462, 9560, self._LIMITS)
        assert "Filling, not losing" in verdict
        assert "WARN" not in verdict

    def test_a_long_lived_store_holding_little_is_a_finding(self):
        from homelab_facts.tools.stores import _retention_verdict

        verdict = _retention_verdict(30 * 86400, 3 * 86400, 90 * 86400, self._LIMITS)
        assert "WARN" in verdict

    def test_missing_readings_are_unavailable_not_healthy(self):
        from homelab_facts.tools.stores import _retention_verdict

        assert "unavailable" in _retention_verdict(None, None, None, self._LIMITS)

    def test_an_unknown_uptime_never_turns_a_shortfall_into_good_news(self):
        from homelab_facts.tools.stores import _retention_verdict

        assert "WARN" in _retention_verdict(30 * 86400, 3 * 86400, None, self._LIMITS)


class TestMetricsStoreReadings:
    def test_uptime_is_filtered_to_the_process_owning_the_tsdb(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        # `job=~".*prometheus.*"` also matches this stack's Grafana, whose job is
        # `...-kube-prometheus-stack-grafana`. Grafana's start time would be read
        # as the TSDB's age and turn a real loss into "filling".
        from homelab_facts.tools import stores

        fake = _wire(monkeypatch, fake_prometheus, {})
        stores.metrics_store_health()
        uptime = [sent for sent in fake.seen if "process_start_time_seconds" in sent]
        assert uptime
        for sent in uptime:
            assert "and on(instance, job) prometheus_tsdb_head_series" in sent

    def test_no_targets_down_is_a_zero_and_not_an_absence(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        # `count(up == 0)` matches nothing when nothing is down. Everywhere else
        # in this server an empty result is `unavailable`; this is the one place
        # it genuinely means zero, and it says so in PromQL.
        from homelab_facts.tools import stores

        fake = _wire(
            monkeypatch,
            fake_prometheus,
            {
                "max(prometheus_tsdb_retention_limit_seconds)": [sample_series(2592000)],
                "max(time() - prometheus_tsdb_lowest_timestamp_seconds)": [sample_series(9462)],
                "count(up)": [sample_series(81)],
                "count(up == 0) or vector(0)": [sample_series(0)],
            },
        )
        out = stores.metrics_store_health()
        assert "0 of 81 scrape targets are down" in out
        assert "count(up == 0) or vector(0)" in fake.seen

    def test_a_down_target_is_named_a_hole_in_another_reading(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        _wire(
            monkeypatch,
            fake_prometheus,
            {
                "count(up)": [sample_series(81)],
                "count(up == 0) or vector(0)": [sample_series(1)],
                "max(prometheus_tsdb_retention_limit_seconds)": [sample_series(2592000)],
            },
        )
        out = stores.metrics_store_health()
        assert "1 of 81" in out
        assert "not a healthy zero" in out

    def test_an_unclean_start_is_stated(self, monkeypatch, fake_prometheus, sample_series):
        from homelab_facts.tools import stores

        _wire(
            monkeypatch,
            fake_prometheus,
            {
                "max(prometheus_tsdb_retention_limit_seconds)": [sample_series(2592000)],
                "min(prometheus_tsdb_clean_start)": [sample_series(0)],
            },
        )
        out = stores.metrics_store_health()
        assert "not clean" in out

    def test_everything_dark_is_unavailable_and_says_why_it_matters(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        _wire(monkeypatch, fake_prometheus, {})
        out = stores.metrics_store_health()
        assert "unavailable" in out
        assert "unverified rather than healthy" in out


class TestSuffixIsNoGuideHere_Either:
    """The same trap `databases.CUMULATIVE_COUNTERS` exists for, in new metrics.

    Verified against Prometheus's metadata API on 2026-08-31:
    `redpanda_raft_leadership_changes`, `api_s3_request_counter` and
    `api_s3_error_counter` are counters carrying no `_total`, while
    `block_resync_errored_blocks` and `redpanda_kafka_under_replicated_replicas`
    are gauges. A suffix rule gets every one of them wrong.
    """

    def test_a_counter_need_not_end_in_total(self):
        from homelab_facts.tools.stores import STORE_COUNTERS

        assert "redpanda_raft_leadership_changes" in STORE_COUNTERS
        assert "api_s3_error_counter" in STORE_COUNTERS

    def test_a_suffix_rule_would_misread_these(self):
        from homelab_facts.tools.stores import STORE_COUNTERS, STORE_GAUGES

        missed = [m for m in STORE_COUNTERS if not m.endswith("_total")]
        assert missed, "a suffix rule must be shown to be wrong, or this test is vacuous"
        assert not (STORE_COUNTERS & STORE_GAUGES)

    def test_the_new_sets_do_not_contradict_the_database_ones(self):
        from homelab_facts.tools.databases import CUMULATIVE_COUNTERS, GAUGES_LOOKING_LIKE_COUNTERS
        from homelab_facts.tools.stores import STORE_COUNTERS, STORE_GAUGES

        assert not (STORE_COUNTERS & GAUGES_LOOKING_LIKE_COUNTERS)
        assert not (STORE_GAUGES & CUMULATIVE_COUNTERS)


class TestBudgets:
    def test_every_new_tool_stays_inside_its_budget(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        pods = [f"node-{index}" for index in range(40)]
        answers = _garage_answers(
            sample_series,
            metadata={pod: 50.0 + index for index, pod in enumerate(pods)},
            data_avail={pod: 10.0 + index for index, pod in enumerate(pods)},
            data_total={pod: 100.0 + index for index, pod in enumerate(pods)},
            data_used={pod: 50.0 + index for index, pod in enumerate(pods)},
        )
        answers[
            stores.used_percent(
                "redpanda_storage_disk_free_bytes",
                "redpanda_storage_disk_total_bytes",
                stores.REDPANDA,
            )
        ] = [sample_series(50.0 + index, pod=pod) for index, pod in enumerate(pods)]
        _wire(monkeypatch, fake_prometheus, answers)
        assert len(stores.object_store_health().encode()) <= stores.OBJECT_STORE_BUDGET
        assert len(stores.stream_health().encode()) <= stores.STREAM_BUDGET
        assert len(stores.metrics_store_health().encode()) <= stores.METRICS_STORE_BUDGET


class TestBuckets:
    """Per-bucket usage is the one reading that needs a credential.

    Prometheus carries no bucket label and no stored-bytes gauge, so with no
    token the section has to say so rather than report an empty bucket list or
    let a total be split across buckets by the model.
    """

    def _garage(self, monkeypatch, client):
        from homelab_facts import settings

        monkeypatch.setattr(settings, "garage", lambda: client)

    def test_no_token_is_a_stated_blind_spot_not_an_empty_list(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        _wire(monkeypatch, fake_prometheus, _one_node(sample_series))
        section = stores.object_store_health().split("## Buckets")[1].split("##")[0]
        assert "unavailable" in section
        assert "no admin token is configured" in section
        assert "Do not split them across buckets" in section

    def test_a_token_yields_size_and_object_count_per_bucket(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        _wire(monkeypatch, fake_prometheus, _one_node(sample_series))
        self._garage(
            monkeypatch,
            FakeGarage(
                buckets=[
                    {"id": "aaa", "globalAliases": ["warehouse"]},
                    {"id": "bbb", "globalAliases": ["backups"]},
                ],
                info={
                    "aaa": {"bytes": 1073741824, "objects": 42, "quotas": {"maxSize": None}},
                    "bbb": {"bytes": 2147483648, "objects": 7, "quotas": {"maxSize": 5368709120}},
                },
            ),
        )
        section = stores.object_store_health().split("## Buckets")[1].split("##")[0]
        assert "warehouse" in section and "1.0GiB" in section and "42" in section
        assert "backups" in section and "2.0GiB" in section and "5.0GiB" in section

    def test_an_unreadable_bucket_is_unavailable_and_never_zero(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        _wire(monkeypatch, fake_prometheus, _one_node(sample_series))
        self._garage(
            monkeypatch,
            FakeGarage(buckets=[{"id": "aaa", "globalAliases": ["warehouse"]}], info={}),
        )
        section = stores.object_store_health().split("## Buckets")[1].split("##")[0]
        assert "unavailable" in section
        assert "could not be read" in section

    def test_a_failed_admin_call_is_an_error_not_no_buckets(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        from mcp_runner.garage import GarageError

        _wire(monkeypatch, fake_prometheus, _one_node(sample_series))
        self._garage(monkeypatch, FakeGarage(error=GarageError("boom")))
        section = stores.object_store_health().split("## Buckets")[1].split("##")[0]
        assert "ERROR" in section

    def test_no_buckets_at_all_is_a_real_answer(
        self, monkeypatch, fake_prometheus, sample_series
    ):
        from homelab_facts.tools import stores

        _wire(monkeypatch, fake_prometheus, _one_node(sample_series))
        self._garage(monkeypatch, FakeGarage(buckets=[]))
        section = stores.object_store_health().split("## Buckets")[1].split("##")[0]
        assert "No buckets exist" in section
        assert "unavailable" not in section


class TestTheAdminClientIsReadOnlyByConstruction:
    """The token grants more than the code can ask for, so the code is the bound.

    A Garage admin token is full admin unless scoped, and a scoped one is a
    deployment choice this server cannot enforce. So the client exposes two GETs
    by name and no generic request method - `CreateBucket` is not expressible
    here whatever the token permits. Same shape as `kube.py`, which strips a
    Secret's payload however a caller asks for it.
    """

    def test_only_the_two_read_operations_are_exposed(self):
        from mcp_runner.garage import Garage

        public = {name for name in vars(Garage) if not name.startswith("_")}
        assert public == {"list_buckets", "bucket_info", "configured"}

    def test_only_two_endpoints_can_be_requested_at_all(self):
        # Assert on the paths the module can send, not on words in its prose:
        # the docstring names the write endpoints precisely to explain why they
        # are unreachable.
        import inspect
        import re

        from mcp_runner import garage

        source = "".join(
            inspect.getsource(function)
            for function in (garage.Garage._get, garage.Garage.list_buckets, garage.Garage.bucket_info)
        )
        assert set(re.findall(r'"(/v2/\w+)"', source)) == {"/v2/ListBuckets", "/v2/GetBucketInfo"}

    def test_the_request_helper_never_takes_a_method(self):
        # No verb argument means no caller can turn a read into a write, whatever
        # the token would permit.
        import inspect

        from mcp_runner.garage import Garage

        assert "method" not in inspect.signature(Garage._get).parameters

    def test_access_key_ids_are_stripped_from_every_bucket(self):
        from mcp_runner.garage import _strip

        stripped = _strip(
            {
                "id": "aaa",
                "bytes": 1,
                "keys": [{"accessKeyId": "GK31c2f218a2e44f485b94239e", "read": True}],
            }
        )
        assert "keys" not in stripped
        assert "GK31c2f218a2e44f485b94239e" not in str(stripped)

    def test_an_absent_token_raises_rather_than_calling_out(self):
        import pytest as _pytest

        from mcp_runner.garage import Garage, GarageUnconfigured

        with _pytest.raises(GarageUnconfigured):
            Garage(url="http://garage.test", token=None).list_buckets()

    def test_the_token_is_never_put_into_an_error_message(self):
        # A 401 body can echo the Authorization header, and this string is
        # rendered into a report that gets posted to Slack.
        import inspect

        from mcp_runner import garage

        source = inspect.getsource(garage.Garage._get)
        raise_line = [line for line in source.splitlines() if "GarageError(" in line]
        assert raise_line
        assert "self.token" not in " ".join(raise_line)
        assert "exc}" not in " ".join(raise_line)

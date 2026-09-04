"""`why_failed()`, `logs()` and `endpoints()` - the tests are the incidents.

Three runs are encoded here. The oracle asked eight questions about grafana,
guessed a label key in five of them, and concluded a running service did not
exist. `sre-sentinel` spent five lookups on one alert and landed none.
`db-steward` spent seven on one Cluster and never wrote down the readings it
already had. Each test below is one half of the chain those runs had to assemble
themselves and no longer do.
"""

from __future__ import annotations

import pytest

from mcp_runner.kube import KubeError, KubeForbidden
from mcp_runner.loki import Entry, LokiError


def pod(name, namespace="monitoring", *, phase="Running", labels=None, owner=None,
        containers=None, created="2026-08-29T10:00:00+00:00", ready=True):
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{name}",
            "creationTimestamp": created,
            "labels": labels or {},
            "ownerReferences": ([{"uid": owner}] if owner else []),
        },
        "status": {
            "phase": phase,
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": containers
            if containers is not None
            else [{"name": "app", "ready": True, "restartCount": 0,
                   "state": {"running": {"startedAt": "2026-08-29T10:00:00+00:00"}}}],
        },
    }


def crashing(name="app", reason="CrashLoopBackOff", restarts=7, last_exit=1):
    return [{
        "name": name,
        "ready": False,
        "restartCount": restarts,
        "state": {"waiting": {"reason": reason, "message": "back-off 5m0s restarting"}},
        "lastState": {"terminated": {"reason": "Error", "exitCode": last_exit}},
    }]


def oomkilled(name="app"):
    return [{
        "name": name,
        "ready": False,
        "restartCount": 3,
        "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
        "lastState": {},
    }]


def deployment(name, namespace="monitoring", selector=None, replicas=1):
    return {
        "metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}",
                     "creationTimestamp": "2026-01-01T00:00:00+00:00"},
        "spec": {"replicas": replicas, "selector": {"matchLabels": selector or {}}},
        "status": {"readyReplicas": 0},
    }


def job(name, namespace="monitoring", succeeded=0, failed=1):
    return {
        "metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}",
                     "creationTimestamp": "2026-08-29T09:00:00+00:00"},
        "spec": {},
        "status": {"succeeded": succeeded, "failed": failed},
    }


def service(name, namespace="monitoring", selector=None):
    return {
        "metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}",
                     "creationTimestamp": "2025-05-10T00:00:00+00:00"},
        "spec": {"type": "ClusterIP", "clusterIP": "10.43.1.1", "ports": [{"port": 80}],
                 "selector": selector if selector is not None else {}},
    }


def event(name, reason="BackOff", kind="Warning", message="Back-off restarting failed container"):
    return {
        "metadata": {"name": f"{name}.1", "namespace": "monitoring"},
        "involvedObject": {"name": name},
        "type": kind,
        "reason": reason,
        "count": 12,
        "message": message,
        "lastTimestamp": "2026-08-29T11:00:00+00:00",
    }


class FakeKube:
    def __init__(self, objects, *, endpoints=None, forbidden=(), logs=None, log_error=None):
        self.objects = objects
        self.endpoints = endpoints if endpoints is not None else []
        self.forbidden = set(forbidden)
        self.logs = logs or {}
        self.log_error = log_error
        self.log_calls = []

    def list(self, api_version, kind, namespace=None, field_selector=None):
        if kind in self.forbidden:
            raise KubeForbidden(f"not permitted to list {kind}")
        if kind == "Endpoints":
            return self.endpoints
        found = self.objects.get(kind, [])
        if namespace:
            found = [o for o in found if (o.get("metadata") or {}).get("namespace") == namespace]
        if field_selector and field_selector.startswith("involvedObject.name="):
            wanted = field_selector.split("=", 1)[1]
            found = [o for o in found if (o.get("involvedObject") or {}).get("name") == wanted]
        return found

    def pod_log(self, namespace, pod, container=None, *, tail_lines=40, previous=False,
                limit_bytes=16384):
        self.log_calls.append((namespace, pod, container, previous))
        if self.log_error:
            raise self.log_error
        return self.logs.get((pod, previous), "")


class FakeLoki:
    def __init__(self, entries=None, error=None):
        self.entries = entries or []
        self.error = error
        self.queries = []

    def query_range(self, query, *, since_seconds=21600, limit=100):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.entries[:limit]


@pytest.fixture
def run(monkeypatch):
    """Call a diagnose tool against a fake cluster and a fake Loki."""

    def go(tool, term, objects, *, loki=None, extra=None, **kwargs):
        from homelab_facts import settings
        from homelab_facts.tools import diagnose

        client = FakeKube(objects, **kwargs)
        fake_loki = loki or FakeLoki()
        monkeypatch.setattr(settings, "kube", lambda: client)
        monkeypatch.setattr(settings, "loki", lambda: fake_loki)
        out = getattr(diagnose, tool)(term, **(extra or {}))
        return out, client, fake_loki

    return go


class TestTheChainRunsInCode:
    """The four calls a run had to sequence itself, now one call."""

    def test_a_crashlooping_container_is_named_as_the_cause(self, run):
        out, _, _ = run(
            "why_failed", "grafana",
            {"Pod": [pod("grafana-0", containers=crashing())],
             "Event": [event("grafana-0")]},
            logs={("grafana-0", True): "panic: config not found"},
        )
        assert "CrashLoopBackOff" in out
        assert "VERDICT:" in out
        assert "panic: config not found" in out

    def test_the_previous_instance_is_what_gets_read(self, run):
        """A crash-looping container is not running, so its current log is empty
        - reading it and reporting nothing is how a crash reads as quiet."""
        out, client, _ = run(
            "why_failed", "grafana",
            {"Pod": [pod("grafana-0", containers=crashing())]},
            logs={("grafana-0", True): "the real error"},
        )
        assert (("monitoring", "grafana-0", "app", True)) in client.log_calls
        assert "the real error" in out

    def test_oomkilled_is_stated_as_the_kernel_stopping_it(self, run):
        out, _, _ = run("why_failed", "loki", {"Pod": [pod("loki-0", containers=oomkilled())]})
        assert "OOMKilled" in out
        assert "memory limit" in out

    def test_a_pending_pod_is_reported_as_unscheduled(self, run):
        out, _, _ = run(
            "why_failed", "big",
            {"Pod": [pod("big-0", phase="Pending", containers=[])],
             "Event": [event("big-0", reason="FailedScheduling",
                             message="0/7 nodes are available: insufficient memory")]},
        )
        assert "Pending" in out
        assert "insufficient memory" in out


class TestPodsAreReachedWithoutGuessingALabel:
    """The guessed selector, removed rather than forbidden.

    `app=grafana` is the plausible wrong guess and `app.kubernetes.io/name` the
    real key. Neither is typed here: the selector is read off the controller.
    """

    def test_a_deployment_reaches_its_pods_through_its_own_selector(self, run):
        real = {"app.kubernetes.io/name": "grafana"}
        out, _, _ = run(
            "why_failed", "grafana",
            {"Deployment": [deployment("grafana", selector=real)],
             "Pod": [pod("grafana-abc", labels=real, containers=crashing()),
                     pod("unrelated-0", labels={"app": "grafana"})]},
        )
        assert "grafana-abc" in out
        assert "unrelated-0" not in out
        assert "spec.selector" in out

    def test_a_job_reaches_its_pods_through_owner_references(self, run):
        out, _, _ = run(
            "why_failed", "grafana setup job",
            {"Job": [job("e-monitoring-grafana-job-setup-postsync")],
             "Pod": [pod("e-monitoring-grafana-job-setup-postsync-xyz",
                         owner="uid-e-monitoring-grafana-job-setup-postsync",
                         phase="Failed", containers=oomkilled()),
                     pod("someone-else-0")]},
        )
        assert "e-monitoring-grafana-job-setup-postsync-xyz" in out
        assert "someone-else-0" not in out
        assert "owned by Job" in out

    def test_an_owner_outranks_its_own_pod_at_equal_match_strength(self, run):
        """`grafana setup job` matches the Job and its pod equally well.

        The Job is the better subject: it answers for every pod it created,
        including the ones that no longer exist, and its own status carries the
        failure count.
        """
        out, _, _ = run(
            "why_failed", "grafana setup job",
            {"Job": [job("e-monitoring-grafana-job-setup-postsync")],
             "Pod": [pod("e-monitoring-grafana-job-setup-postsync-xyz",
                         owner="uid-e-monitoring-grafana-job-setup-postsync",
                         created="2026-08-29T23:00:00+00:00")]},
        )
        assert "-> Job monitoring/e-monitoring-grafana-job-setup-postsync" in out

    def test_a_service_reaches_pods_through_its_own_selector(self, run):
        selector = {"app.kubernetes.io/name": "grafana"}
        out, _, _ = run(
            "endpoints", "grafana",
            {"Service": [service("grafana", selector=selector)],
             "Pod": [pod("grafana-0", labels=selector)]},
            endpoints=[{"subsets": [{"addresses": [{"ip": "10.42.0.1"}]}]}],
        )
        assert "grafana-0" in out
        assert "connections succeed" in out


class TestAbsenceStaysAbsence:
    """Every oracle failure so far is a negative claim built from an empty
    result. None of these may produce one."""

    def test_no_match_is_searched_and_not_matched(self, run):
        out, _, _ = run("why_failed", "nothing-like-this", {})
        assert "searched-and-not-matched" in out
        assert "not proof that nothing related exists" in out

    def test_a_workload_with_no_pods_is_a_finding_not_a_missing_object(self, run):
        out, _, _ = run(
            "why_failed", "scaled-down",
            {"Deployment": [deployment("scaled-down", selector={"a": "b"}, replicas=0)]},
        )
        assert "cause not determined" in out
        # "does not exist" may appear only inside the sentence forbidding it.
        assert "not evidence that scaled-down does not exist" in out

    def test_a_forbidden_kind_is_named_rather_than_counted_as_absent(self, run):
        out, _, _ = run("why_failed", "grafana", {}, forbidden={"Pod", "Job"})
        assert "NOT SEARCHED" in out

    def test_everything_forbidden_is_an_error(self, run):
        from homelab_facts.tools import diagnose

        out, _, _ = run("why_failed", "grafana", {},
                        forbidden={kind for _, kind in diagnose._FAILABLE})
        assert out.startswith("ERROR:")
        assert "permission failure, not an empty cluster" in out

    def test_unreadable_events_say_so_rather_than_reporting_none(self, run):
        out, _, _ = run("why_failed", "grafana",
                        {"Pod": [pod("grafana-0", containers=crashing())]},
                        forbidden={"Event"})
        assert "NOT READ" in out

    def test_a_loki_failure_never_reads_as_no_logs(self, run):
        out, _, _ = run(
            "logs", "grafana",
            {"Pod": [pod("grafana-0")]},
            log_error=KubeError("container is waiting to start"),
            loki=FakeLoki(error=LokiError("connection refused")),
        )
        assert "retained logs are unknown" in out
        assert "no logs" not in out.lower()

    def test_an_empty_loki_window_says_nothing_was_retained(self, run):
        out, _, _ = run(
            "logs", "grafana",
            {"Pod": [pod("grafana-0")]},
            log_error=KubeError("previous terminated container not found"),
        )
        assert "none was retained" in out
        assert "not that the container was quiet" in out


class TestNothingFailingIsAnAnswer:
    """A format that demands a fault is how invented ones get written -
    `endpoint-warden` relabelled a memory figure as disk to fill a column."""

    def test_a_healthy_workload_says_nothing_is_failing(self, run):
        out, _, _ = run("why_failed", "grafana", {"Pod": [pod("grafana-0")]})
        assert "nothing here is failing right now" in out

    def test_a_succeeded_job_is_not_reported_as_a_failure(self, run):
        out, _, _ = run(
            "why_failed", "setup",
            {"Job": [job("setup", succeeded=1, failed=0)],
             "Pod": [pod("setup-xyz", owner="uid-setup", phase="Succeeded",
                         containers=[{"name": "app", "ready": False, "restartCount": 0,
                                      "state": {"terminated": {"reason": "Completed",
                                                               "exitCode": 0}}}])]},
        )
        assert "nothing here is failing right now" in out


class TestLogsResolveEverythingThemselves:
    def test_the_newest_pod_is_read_and_the_others_are_named(self, run):
        out, _, _ = run(
            "logs", "grafana",
            {"Deployment": [deployment("grafana", selector={"k": "v"})],
             "Pod": [pod("grafana-old", labels={"k": "v"}, created="2026-01-01T00:00:00+00:00"),
                     pod("grafana-new", labels={"k": "v"}, created="2026-08-29T00:00:00+00:00")]},
            logs={("grafana-new", False): "hello"},
        )
        assert "Reading the newest, grafana-new" in out
        assert "grafana-old" in out
        assert "hello" in out

    def test_contains_filters_lines_literally(self, run):
        out, _, _ = run(
            "logs", "grafana", {"Pod": [pod("grafana-0")]},
            extra={"contains": "ERROR"},
            logs={("grafana-0", False): "line one\nan ERROR happened\nline three"},
        )
        assert "an ERROR happened" in out
        assert "line three" not in out

    def test_a_deleted_pod_falls_back_to_the_namespace_stream(self, run):
        out, _, loki = run(
            "logs", "gone",
            {"Deployment": [deployment("gone", selector={"k": "v"})]},
            loki=FakeLoki([Entry(1, {}, "a line from loki")]),
        )
        assert "a line from loki" in out
        assert loki.queries == ['{namespace="monitoring"}']

    def test_the_source_and_window_are_always_stated(self, run):
        out, _, _ = run("logs", "grafana", {"Pod": [pod("grafana-0")]},
                        logs={("grafana-0", False): "x"})
        assert "source:" in out

    def test_loki_is_queried_with_resolved_values_only(self, run):
        _, _, loki = run(
            "logs", "grafana", {"Pod": [pod("grafana-0")]},
            log_error=KubeError("not found"),
            loki=FakeLoki([Entry(1, {}, "line")]),
        )
        assert loki.queries == ['{namespace="monitoring",pod="grafana-0",container="app"}']


class TestEndpointsAnswerCurlExitSeven:
    def test_no_endpoint_is_refused_rather_than_missing(self, run):
        out, _, _ = run("endpoints", "grafana", {"Service": [service("grafana")]}, endpoints=[])
        assert "nothing behind it" in out
        assert "never a missing Service" in out

    def test_an_empty_selector_explains_why_nothing_is_behind_it(self, run):
        out, _, _ = run("endpoints", "grafana",
                        {"Service": [service("grafana", selector={"k": "v"})],
                         "Pod": [pod("other-0", labels={"k": "other"})]},
                        endpoints=[])
        assert "No pod carries the labels this Service selects" in out

    def test_an_ingressroute_reports_its_hostname(self, run):
        route = {
            "metadata": {"name": "grafana", "namespace": "monitoring",
                         "creationTimestamp": "2026-01-01T00:00:00+00:00"},
            "spec": {"entryPoints": ["websecure"],
                     "routes": [{"match": "Host(`grafana.example.com`)"}]},
        }
        out, _, _ = run("endpoints", "grafana", {"IngressRoute": [route]})
        assert "grafana.example.com" in out


class TestTheAnswerIsBounded:
    """One ~16KB result reproducibly ends a run with `terminal turn had empty
    text`, and that is not context overflow."""

    def test_why_failed_stays_within_budget(self, run):
        from homelab_facts.tools import diagnose

        pods = [pod(f"noisy-{i}", labels={"k": "v"}, containers=crashing()) for i in range(50)]
        out, _, _ = run(
            "why_failed", "noisy",
            {"Deployment": [deployment("noisy", selector={"k": "v"})], "Pod": pods,
             "Event": [event(f"noisy-{i}", message="x" * 400) for i in range(30)]},
            logs={(f"noisy-{i}", True): "y" * 5000 for i in range(50)},
        )
        assert len(out.encode()) <= diagnose.BUDGET

    def test_logs_stay_within_budget(self, run):
        from homelab_facts.tools import diagnose

        out, _, _ = run("logs", "chatty", {"Pod": [pod("chatty-0")]},
                        logs={("chatty-0", False): "\n".join("z" * 500 for _ in range(200))})
        assert len(out.encode()) <= diagnose.LOGS_BUDGET

    def test_a_single_long_line_is_cut_rather_than_dropped(self, run):
        out, _, _ = run("logs", "chatty", {"Pod": [pod("chatty-0")]},
                        logs={("chatty-0", False): "q" * 5000})
        assert "[line cut]" in out

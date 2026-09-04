"""`find_object()` - the tests are the incident.

A person asked about `grafana-setup-job`. No object has that name; the pods are
`e-monitoring-grafana-job-setup<hash>-postsync-<epoch>-<suffix>` in `monitoring`,
and the run that guessed label selectors instead concluded grafana does not
exist. Every test below is one half of not repeating that.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def find(monkeypatch):
    """Run `find_object` against a fake cluster, keyed by kind."""

    def run(objects: dict[str, list[dict]], term: str, endpoints=None, forbidden=()):
        from homelab_facts import settings
        from homelab_facts.tools import lookup

        from mcp_runner.kube import KubeForbidden

        class Kube:
            def list(self, api_version, kind, namespace=None, field_selector=None):
                if kind in forbidden:
                    raise KubeForbidden(f"not permitted to list {kind}")
                if kind == "Endpoints":
                    return endpoints if endpoints is not None else []
                return objects.get(kind, [])

        monkeypatch.setattr(settings, "kube", lambda: Kube())
        return lookup.find_object(term)

    return run


def pod(name, namespace="monitoring", created="2026-08-29T10:00:00+00:00", phase="Succeeded"):
    return {
        "metadata": {"name": name, "namespace": namespace, "creationTimestamp": created},
        "status": {"phase": phase, "containerStatuses": [{"ready": False, "restartCount": 0}]},
    }


def service(name, namespace="monitoring"):
    return {
        "metadata": {"name": name, "namespace": namespace, "creationTimestamp": "2025-05-10T00:00:00+00:00"},
        "spec": {"type": "ClusterIP", "clusterIP": "10.43.175.110", "ports": [{"port": 80}]},
    }


class TestFindingWhatNobodyCanType:
    def test_every_word_matches_a_generated_name(self, find):
        """The incident, exactly: three words, none of them the whole name."""
        out = find(
            {"Pod": [pod("e-monitoring-grafana-job-setupe682e6d-postsync-1788000739-gc9tx")]},
            "grafana-setup-job",
        )
        assert "e-monitoring-grafana-job-setupe682e6d-postsync-1788000739-gc9tx" in out
        assert "monitoring" in out
        assert "words" in out

    def test_a_substring_matches(self, find):
        out = find({"Service": [service("datahub-local-core-kube-prometheus-stack-grafana")]}, "grafana")
        assert "contains" in out

    def test_an_exact_name_is_labelled_exact(self, find):
        out = find({"Service": [service("grafana")]}, "grafana")
        assert "exact" in out

    def test_punctuation_and_case_are_ignored(self, find):
        out = find({"Pod": [pod("my-grafana-setup-0")]}, "Grafana Setup")
        assert "my-grafana-setup-0" in out

    def test_a_word_that_is_not_there_does_not_match(self, find):
        """`words` is an AND. A partial overlap must not produce a false hit."""
        out = find({"Pod": [pod("loki-setup-job-1")]}, "grafana setup job")
        assert "loki-setup-job-1" not in out


class TestAbsenceIsSearchedNotProven:
    def test_no_match_says_what_was_searched(self, find):
        out = find({}, "nothing-like-this")
        assert "searched-and-not-matched" in out
        # The whole point: the model may not upgrade this to "does not exist".
        assert "not proof that nothing related exists" in out

    def test_no_match_names_the_kinds(self, find):
        out = find({}, "nothing-like-this")
        for kind in ("Pod", "Service", "Job", "Namespace"):
            assert kind in out


class TestTheAnswerIsBounded:
    def test_many_matches_stay_within_budget_and_say_so(self, find):
        from homelab_facts.tools import lookup

        pods = [
            pod(f"e-monitoring-grafana-job-setup{index:04d}-postsync-{index}", created=f"2026-08-{index % 28 + 1:02d}T00:00:00+00:00")
            for index in range(200)
        ]
        out = find({"Pod": pods}, "grafana setup")
        assert len(out.encode()) <= lookup.BUDGET
        assert "more Pods match and are not listed" in out

    def test_newest_first(self, find):
        pods = [
            pod("old-grafana-setup", created="2026-01-01T00:00:00+00:00"),
            pod("new-grafana-setup", created="2026-08-29T00:00:00+00:00"),
        ]
        out = find({"Pod": pods}, "grafana setup")
        assert out.index("new-grafana-setup") < out.index("old-grafana-setup")


class TestServiceEndpoints:
    """`curl: (7)` on a name that resolves is an endpointless Service."""

    def test_a_service_with_no_endpoints_says_connections_are_refused(self, find):
        out = find({"Service": [service("grafana")]}, "grafana", endpoints=[])
        assert "nothing behind it" in out

    def test_a_ready_endpoint_says_connections_succeed(self, find):
        out = find(
            {"Service": [service("grafana")]},
            "grafana",
            endpoints=[{"subsets": [{"addresses": [{"ip": "10.42.0.1"}]}]}],
        )
        assert "1 ready, 0 not ready" in out
        assert "connections succeed" in out


class TestSecretsAreNotSearchable:
    def test_no_secret_kind_is_ever_listed(self):
        """A name search over Secrets enumerates credentials and answers nothing."""
        from homelab_facts.tools import lookup

        kinds = {kind for _, kind in lookup._KINDS}
        assert "Secret" not in kinds
        assert "ConfigMap" not in kinds


class TestArguments:
    def test_an_empty_term_is_an_error_not_an_empty_search(self, find):
        assert find({}, "  ").startswith("ERROR:")


class TestAForbiddenKindIsNotASearchedKind:
    """The bug that shipped: a 403 came back as `[]` and read as "not found".

    The ServiceAccount could not list Pods, so a cluster running grafana was
    reported as having no grafana object at all. A kind that could not be read
    is a blind spot and has to be named as one.
    """

    def test_a_forbidden_kind_is_named_not_counted_as_absent(self, find):
        out = find({}, "grafana", forbidden={"Pod", "Service"})
        assert "NOT SEARCHED" in out
        assert "Pod" in out and "Service" in out

    def test_a_forbidden_kind_is_absent_from_the_searched_list(self, find):
        out = find({"Job": []}, "grafana", forbidden={"Pod"})
        searched_line = out.splitlines()[0]
        assert "Pod" not in searched_line

    def test_everything_forbidden_is_an_error_not_an_empty_result(self, find):
        from homelab_facts.tools import lookup

        out = find({}, "grafana", forbidden={kind for _, kind in lookup._KINDS})
        assert out.startswith("ERROR:")
        assert "permission failure, not an empty cluster" in out

    def test_a_match_still_warns_about_what_was_not_searched(self, find):
        out = find({"Service": [service("grafana")]}, "grafana", forbidden={"Pod"})
        assert "grafana" in out
        assert "NOT SEARCHED" in out


class TestKubeListItself:
    def test_a_403_raises_rather_than_returning_empty(self, monkeypatch):
        from mcp_runner import kube

        class Forbidden(Exception):
            status = 403

        class Resource:
            def get(self, **kwargs):
                raise Forbidden()

        class Resources:
            def get(self, **kwargs):
                return Resource()

        client = kube.Kube()
        monkeypatch.setattr(client, "_dynamic", lambda: type("D", (), {"resources": Resources()})())
        with pytest.raises(kube.KubeForbidden):
            client.list("v1", "Pod")

    def test_any_other_failure_still_degrades_to_empty(self, monkeypatch):
        """An absent CRD is normal here and must stay a quiet empty list."""
        from mcp_runner import kube

        class Resource:
            def get(self, **kwargs):
                raise RuntimeError("connection reset")

        class Resources:
            def get(self, **kwargs):
                return Resource()

        client = kube.Kube()
        monkeypatch.setattr(client, "_dynamic", lambda: type("D", (), {"resources": Resources()})())
        assert client.list("v1", "Pod") == []


class TestTraefikIsHowThisClusterRoutes:
    """35 IngressRoutes, zero `networking.k8s.io` Ingresses.

    A tool that searched only `Ingress` could never answer "what URL is X on"
    here - the `valkey_*`/`redis_*` lesson in a new place: the standard name is
    not the one in use.
    """

    def _route(self, name, host, namespace="monitoring"):
        return {
            "metadata": {"name": name, "namespace": namespace, "creationTimestamp": "2026-01-01T00:00:00+00:00"},
            "spec": {
                "entryPoints": ["websecure"],
                "routes": [
                    {"match": f"Host(`{host}`)", "services": [{"name": name}]},
                    {"match": f"Host(`{host}`) && PathPrefix(`/oauth2/`)"},
                ],
            },
        }

    def test_an_ingressroute_is_found_and_reports_its_hostname(self, find):
        out = find({"IngressRoute": [self._route("grafana", "grafana.homelab.example.com")]}, "grafana")
        assert "IngressRoute" in out
        assert "grafana.homelab.example.com" in out
        assert "websecure" in out

    def test_a_repeated_host_is_listed_once(self, find):
        out = find({"IngressRoute": [self._route("grafana", "g.example.com")]}, "grafana")
        assert out.count("g.example.com") == 1

    def test_a_route_with_no_host_rule_says_so(self, find):
        route = self._route("grafana", "x")
        route["spec"]["routes"] = [{"match": "PathPrefix(`/api`)"}]
        out = find({"IngressRoute": [route]}, "grafana")
        assert "no Host() rule" in out

    def test_plain_ingress_is_still_searched(self):
        """Zero today is not zero forever, and the kind list is derived from nothing."""
        from homelab_facts.tools import lookup

        kinds = {kind for _, kind in lookup._KINDS}
        assert {"Ingress", "IngressRoute"} <= kinds

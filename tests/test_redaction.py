"""A Secret's contents must not be reachable through this server.

Core's `mcp-k8s` ServiceAccount holds `apiGroups: ["*"], resources: ["*"],
verbs: [get,list,watch]`, deliberately widened so agents can read velero, cnpg,
longhorn and cert-manager objects. The cost is that a `get` on a Secret returns
its base64 values in full - verified against `mcp-slack-token` on 2026-08-24 -
which is why `k8s_resources_get` is banned for personas.

This server keeps the property structurally rather than by policy.
"""

from __future__ import annotations

from mcp_runner.kube import _strip, summarise

SECRET = {
    "metadata": {
        "name": "mcp-slack-token",
        "namespace": "automation",
        "creationTimestamp": "2026-08-01T00:00:00Z",
        "annotations": {
            "kubectl.kubernetes.io/last-applied-configuration": '{"data":{"token":"c3VwZXI="}}',
            "keep": "this",
        },
    },
    "type": "Opaque",
    "data": {"token": "c3VwZXJzZWNyZXQ="},
    "stringData": {"other": "plaintext"},
}


class TestStripAtTheBoundary:
    def test_data_never_survives_a_list(self):
        assert "data" not in _strip(dict(SECRET))

    def test_string_data_never_survives_either(self):
        assert "stringData" not in _strip(dict(SECRET))

    def test_the_last_applied_annotation_is_dropped(self):
        # It is a full copy of the object and has carried inlined credentials.
        out = _strip(
            {
                "metadata": {
                    "annotations": dict(SECRET["metadata"]["annotations"]),
                }
            }
        )
        annotations = out["metadata"]["annotations"]
        assert "kubectl.kubernetes.io/last-applied-configuration" not in annotations

    def test_harmless_annotations_are_kept(self):
        out = _strip({"metadata": {"annotations": dict(SECRET["metadata"]["annotations"])}})
        assert out["metadata"]["annotations"]["keep"] == "this"

    def test_metadata_and_type_survive_so_the_check_still_works(self):
        out = _strip(dict(SECRET))
        assert out["metadata"]["name"] == "mcp-slack-token"
        assert out["type"] == "Opaque"

    def test_no_secret_value_appears_anywhere_in_the_output(self):
        out = repr(_strip(dict(SECRET)))
        assert "c3VwZXJzZWNyZXQ=" not in out
        assert "plaintext" not in out


class TestSummarise:
    def test_a_caller_cannot_project_the_data_field(self):
        out = summarise(dict(SECRET), {"data": ("data",)})
        assert "data" not in out

    def test_a_missing_path_is_none_not_an_exception(self):
        # A half-populated status is the normal state of a fresh object.
        out = summarise({"metadata": {"name": "x"}}, {"phase": ("status", "phase")})
        assert out["phase"] is None

    def test_a_named_path_is_projected(self):
        out = summarise(
            {"metadata": {"name": "x"}, "status": {"phase": "Completed"}},
            {"phase": ("status", "phase")},
        )
        assert out["phase"] == "Completed"


class TestSecretsAreRequestedNarrowly:
    def test_cert_expiry_uses_a_field_selector(self):
        # An unfiltered cluster-wide Secret list transfers every value in every
        # namespace - 25 MB on this cluster, which broke the connection outright.
        import inspect

        from homelab_facts.tools import lifecycle

        source = inspect.getsource(lifecycle.cert_expiry)
        assert "field_selector=" in source
        assert "kubernetes.io/service-account-token" in source

"""Deriving hardware classes from the cluster instead of a config file.

An earlier version of this server carried a `hardware_classes.yaml` naming all
seven machines and which sensors each had. That is a second copy of the cluster:
rename a node or move a disk and it goes stale silently, producing the exact
failure the server exists to prevent - a row of `unavailable` for a machine whose
figures were available all along.
"""

from __future__ import annotations

from mcp_runner.fleet import (
    class_name,
    common_prefix,
    group_by_class,
    kernel_drift,
    kernel_flavour,
    kernel_version,
    version_gap,
    version_key,
)

# The kernels running on this fleet on 2026-08-25, read off `kubectl get nodes`.
# Four platforms on four trees, which is what makes fleet-wide comparison wrong.
KERNELS = {
    "datahublocal-amd-1": "6.12.96+deb13-amd64",
    "datahublocal-amd-2": "6.12.101+deb13-amd64",
    "datahublocal-nas": "6.12.15-production+truenas",
    "datahublocal-orpi-0": "7.1.2-edge-rockchip64",
    "datahublocal-orpi-1": "6.1.115-vendor-rk35xx",
    "datahublocal-orpi-2": "6.1.115-vendor-rk35xx",
    "datahublocal-orpi-3": "6.1.115-vendor-rk35xx",
}
MACHINES = {
    "datahublocal-amd-1": "x86_64",
    "datahublocal-amd-2": "x86_64",
    "datahublocal-nas": "x86_64",
    "datahublocal-orpi-0": "aarch64",
    "datahublocal-orpi-1": "aarch64",
    "datahublocal-orpi-2": "aarch64",
    "datahublocal-orpi-3": "aarch64",
}


class TestKernelParsing:
    def test_flavour_drops_the_numeric_version(self):
        assert kernel_flavour("6.12.96+deb13-amd64") == "deb13-amd64"
        assert kernel_flavour("6.1.115-vendor-rk35xx") == "vendor-rk35xx"
        assert kernel_flavour("7.1.2-edge-rockchip64") == "edge-rockchip64"
        assert kernel_flavour("6.12.15-production+truenas") == "production+truenas"

    def test_version_keeps_only_the_numeric_head(self):
        assert kernel_version("6.12.96+deb13-amd64") == "6.12.96"
        assert kernel_version("6.1.115-vendor-rk35xx") == "6.1.115"

    def test_two_patch_levels_of_one_tree_share_a_flavour(self):
        # This is the whole mechanism: same flavour means comparable.
        assert kernel_flavour("6.12.96+deb13-amd64") == kernel_flavour("6.12.101+deb13-amd64")

    def test_different_trees_never_share_a_flavour(self):
        assert kernel_flavour("7.1.2-edge-rockchip64") != kernel_flavour("6.1.115-vendor-rk35xx")

    def test_version_ordering_is_numeric_not_lexical(self):
        # "6.12.96" > "6.12.101" as strings, which would name the wrong node as
        # the one trailing its class.
        assert version_key("6.12.101") > version_key("6.12.96")
        # And the naive comparison this replaces really is backwards.
        assert max(["6.12.101", "6.12.96"]) == "6.12.96"

    def test_an_odd_release_string_does_not_raise(self):
        assert kernel_flavour("") == "unknown"
        assert kernel_flavour("weird-kernel") == "weird-kernel"
        assert version_key("weird") == ()

    def test_class_includes_architecture(self):
        # Same flavour on two architectures is still two classes.
        assert class_name("6.1.0-x", "aarch64") != class_name("6.1.0-x", "x86_64")


class TestClassDerivation:
    def test_this_fleet_derives_into_four_classes(self):
        grouped = group_by_class(KERNELS, MACHINES)
        assert len(grouped) == 4

    def test_the_three_identical_boards_group_together(self):
        grouped = group_by_class(KERNELS, MACHINES)
        rk35xx = next(members for name, members in grouped.items() if "rk35xx" in name)
        assert len(rk35xx) == 3

    def test_derivation_needs_no_node_names(self):
        # Rename every machine and the classes are unchanged.
        renamed = {f"host{index}": release for index, release in enumerate(KERNELS.values())}
        machines = {f"host{index}": arch for index, arch in enumerate(MACHINES.values())}
        assert len(group_by_class(renamed, machines)) == 4

    def test_a_new_node_joins_its_class_with_no_config_change(self):
        kernels = dict(KERNELS, **{"datahublocal-orpi-4": "6.1.115-vendor-rk35xx"})
        machines = dict(MACHINES, **{"datahublocal-orpi-4": "aarch64"})
        grouped = group_by_class(kernels, machines)
        rk35xx = next(members for name, members in grouped.items() if "rk35xx" in name)
        assert len(rk35xx) == 4
        assert len(grouped) == 4


class TestKernelDrift:
    """orpi-0's kernel was reported as drift every run for days.

    Different SoC families on different trees cannot converge, so the finding
    could never be actioned and never cleared - and because a non-empty findings
    section was itself a change condition, it forced a Slack post every time.
    """

    def test_the_one_real_drift_in_this_fleet_is_found(self):
        results = {group: behind for group, _members, behind in kernel_drift(KERNELS, MACHINES)}
        amd = next(behind for group, behind in results.items() if "deb13" in group)
        assert amd == ["datahublocal-amd-1"]  # 6.12.96 behind amd-2's 6.12.101

    def test_a_class_of_one_is_never_drift(self):
        for group, members, behind in kernel_drift(KERNELS, MACHINES):
            if len(members) == 1:
                assert behind == [], group

    def test_identical_versions_in_a_class_are_not_drift(self):
        results = {group: behind for group, _members, behind in kernel_drift(KERNELS, MACHINES)}
        rk35xx = next(behind for group, behind in results.items() if "rk35xx" in group)
        assert rk35xx == []

    def test_nodes_of_different_classes_are_never_compared(self):
        # orpi-0 (7.1.2) and orpi-1 (6.1.115) differ by a whole major version and
        # must still not appear as drift against each other.
        all_behind = [node for _g, _m, behind in kernel_drift(KERNELS, MACHINES) for node in behind]
        assert "datahublocal-orpi-0" not in all_behind
        assert "datahublocal-orpi-1" not in all_behind

    def test_exactly_one_node_in_the_whole_fleet_is_behind(self):
        all_behind = [node for _g, _m, behind in kernel_drift(KERNELS, MACHINES) for node in behind]
        assert all_behind == ["datahublocal-amd-1"]


class TestCommonPrefix:
    def test_the_shared_cluster_prefix_is_derived(self):
        assert common_prefix(list(KERNELS)) == "datahublocal-"

    def test_no_shared_prefix_yields_nothing(self):
        assert common_prefix(["alpha", "beta"]) == ""

    def test_a_single_node_yields_nothing(self):
        assert common_prefix(["only-one"]) == ""

    def test_prefix_is_cut_at_a_separator_not_mid_word(self):
        # "node-a1"/"node-a2" must not shorten to "1"/"2".
        assert common_prefix(["node-a1", "node-a2"]) == "node-"


class TestVersionGap:
    """The gap is computed here because the model invents it otherwise.

    Asked about the fleet's one real drift pair a 4B model answered "6 months
    behind" and "15 minor versions"; the truth is five patch releases inside one
    series, and a kernel string carries no date at all.
    """

    def test_the_fleets_real_pair_is_five_patch_releases(self):
        assert version_gap("6.12.96", "6.12.101") == "5 patch releases behind, both on 6.12"

    def test_it_is_never_expressed_in_minor_versions(self):
        # The wrong answer the model gave. Same series, so "minor" cannot appear.
        assert "minor" not in version_gap("6.12.96", "6.12.101")

    def test_a_single_step_is_singular(self):
        assert version_gap("6.12.100", "6.12.101") == "1 patch release behind, both on 6.12"

    def test_a_minor_step_says_series_and_is_not_pluralised_wrongly(self):
        assert version_gap("6.11.0", "6.12.3") == "1 minor series behind, both on 6"
        assert version_gap("6.10.0", "6.12.0") == "2 minor series behind, both on 6"

    def test_a_major_step_has_nothing_in_common(self):
        assert version_gap("5.10.1", "6.0.0") == "1 major version behind"

    def test_equal_versions_have_no_gap(self):
        assert version_gap("6.1.115", "6.1.115") == ""

    def test_a_node_ahead_is_not_behind(self):
        assert version_gap("6.12.101", "6.12.96") == ""

    def test_an_unparseable_release_yields_no_invented_gap(self):
        assert version_gap("", "6.1.1") == ""
        assert version_gap("6.1.1", "") == ""

    def test_versions_of_different_length_compare_by_position(self):
        assert version_gap("6.12", "6.12.5") == "5 patch releases behind, both on 6.12"

    def test_string_comparison_would_get_this_backwards(self):
        # "6.12.96" > "6.12.101" as strings; as versions it is behind.
        assert version_gap("6.12.96", "6.12.101") != ""
        assert version_gap("6.12.101", "6.12.96") == ""

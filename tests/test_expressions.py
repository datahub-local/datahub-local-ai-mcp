"""The expression builders — where nine prose validators became unit tests.

`scripts/validate.py` had grown roughly 600 lines of regex policing English in
system prompts: `_check_fill_direction`, `_check_counter_window`,
`_check_endtime_literal`, `_check_datasource_uid`, `_check_nodename_join` and
four more. Each existed because a prompt could describe a query wrongly. A
prompt can no longer describe a query at all, so each check becomes an assertion
about the expression the server actually sends.
"""

from __future__ import annotations

import re
from typing import ClassVar

from homelab_facts.tools.volumes import build_expression

from mcp_runner.prometheus import by_nodename, increase_, rate_percent, used_percent


class TestFillDirection:
    """Was `_check_fill_direction`.

    `available / capacity` is the fraction FREE. Reporting it as fill inverts
    every finding: it flags the emptiest volumes and can never flag a full one,
    which called a 2%-used volume "97.9% full, write operations failing" on every
    run for days.
    """

    def test_used_percent_inverts_the_ratio(self):
        assert used_percent("avail", "cap") == "100 * (1 - avail / cap)"

    def test_used_percent_is_never_a_bare_ratio(self):
        expression = used_percent("kubelet_volume_stats_available_bytes",
                                  "kubelet_volume_stats_capacity_bytes")
        assert "1 -" in expression
        assert not re.match(r"^\s*\w+\s*/\s*\w+", expression)

    def test_a_full_volume_scores_high_and_an_empty_one_low(self):
        # The arithmetic, not just the string: 5% free must read as 95% used.
        def evaluate(avail: float, cap: float) -> float:
            return eval(used_percent(str(avail), str(cap)))

        assert evaluate(5, 100) == 95.0
        assert abs(evaluate(98, 100) - 2.0) < 1e-9

    def test_volume_expression_keeps_the_storage_class_join(self):
        # The nfs claims all report one shared 1.9 TB capacity, so a per-volume
        # percentage there is the share's fill repeated once per claim.
        expression = build_expression(["longhorn", "longhorn-no-replica"])
        assert "group_left(storageclass)" in expression
        assert "on(namespace, persistentvolumeclaim)" in expression
        assert 'storageclass=~"longhorn|longhorn-no-replica"' in expression

    def test_volume_expression_is_used_not_free(self):
        expression = build_expression(["longhorn"])
        assert expression.startswith("100 * (1 - ")


class TestCounterWindow:
    """Was `_check_counter_window`.

    A model given `increase(m[1h])` dropped the wrapper, sent `m[1h]`, and
    labelled the raw counter as the increase. The wrapper is now applied by code.
    """

    def test_increase_wraps_the_metric(self):
        assert increase_("m", "1h") == "increase(m[1h])"

    def test_increase_is_never_a_bare_range_selector(self):
        expression = increase_("cnpg_pg_stat_archiver_failed_count")
        assert expression.startswith("increase(")
        assert not expression.startswith("cnpg_")

    def test_rate_percent_wraps_and_scales(self):
        assert rate_percent("m", "5m") == "100 * rate(m[5m])"


class TestNodenameJoin:
    """Was `_check_nodename_join`.

    No `node_*` series carries a machine name - they are keyed by `instance`, an
    IP and port. The only hostname anywhere is `nodename` on `node_uname_info`.
    Without the join the model improvised `by (node)`, which is neither valid
    PromQL nor an existing label, and a table came out wrong in five ways at once.
    """

    def test_join_is_attached(self):
        expression = by_nodename("node_filesystem_avail_bytes")
        assert "on(instance) group_left(nodename) node_uname_info" in expression

    def test_join_aggregates_by_nodename_not_node(self):
        expression = by_nodename("m")
        assert "by (nodename)" in expression
        assert "by (node)" not in expression

    def test_aggregator_is_selectable_for_worst_case_readings(self):
        # SMART health is a `min` - one failing drive on a node is the finding -
        # while a temperature is a `max`.
        assert by_nodename("m", "min").startswith("min by (nodename)")
        assert by_nodename("m").startswith("max by (nodename)")

    def test_every_node_query_in_the_fleet_tool_carries_the_join(self):
        from homelab_facts.tools import nodes

        for name, expression in nodes._QUERIES.items():
            assert "group_left(nodename) node_uname_info" in expression, name
            assert "by (nodename)" in expression, name

    def test_every_scoped_query_and_its_probe_carry_the_join(self):
        from homelab_facts.tools import nodes

        for name, (expression, probe, _agg) in nodes._SCOPED.items():
            assert "group_left(nodename) node_uname_info" in expression, name
            assert "group_left(nodename) node_uname_info" in probe, name


class TestNoDatasourceOrEndTime:
    """Was `_check_datasource_uid` and `_check_endtime_literal`.

    Grafana's Prometheus datasource needed a uid, and this Grafana serves three:
    Prometheus's uid is the bare word `prometheus` while Loki's is the hex string
    `P8E80F9AEF21F6940`. A 4B model read the hex as the real identifier and the
    word as a placeholder, so every query went to Loki and answered 404. It also
    needed `endTime`, whose only correct value was the literal word `now`; one
    run sent a unix timestamp for September 2024 and read six empty results as a
    dead fleet.

    Querying Prometheus directly removes both arguments from existence.
    """

    def test_no_expression_builder_mentions_a_datasource(self):
        for expression in (
            used_percent("a", "b"),
            increase_("m"),
            rate_percent("m"),
            by_nodename("m"),
            build_expression(["longhorn"]),
        ):
            assert "datasource" not in expression.lower()
            assert "prometheus" not in expression.lower()

    def test_promql_tool_takes_only_an_expression_and_a_window(self):
        from homelab_facts.tools.raw import SCHEMA

        assert set(SCHEMA["properties"]) == {"expr", "window"}
        assert SCHEMA["required"] == ["expr"]

    # The tools that do take an argument. Every one of them takes free text
    # where any string is valid, which is the actual rule: an argument is safe
    # when there is no format to get wrong. `chatId`, `datasourceUid` and
    # `endTime` each had exactly one correct shape, and each was copied wrong.
    # `contains` is a literal substring rather than a regex for the same reason -
    # a bad regex is a query error, where a bad substring is just no match.
    _FREE_TEXT_ARGUMENTS: ClassVar[dict[str, set[str]]] = {
        "promql": {"expr", "window"},
        "find_object": {"term"},
        "why_failed": {"term"},
        "logs": {"term", "contains"},
        "endpoints": {"term"},
    }

    def test_no_fact_tool_takes_any_argument_at_all(self):
        # A tool with no arguments cannot be called with the wrong ones.
        from mcp_runner.__main__ import build_registry

        registry = build_registry("homelab_facts")
        for name, tool in registry.tools.items():
            allowed = self._FREE_TEXT_ARGUMENTS.get(name, set())
            assert set(tool.schema.get("properties") or {}) == allowed, name

    def test_no_tool_asks_for_a_value_only_the_cluster_knows(self):
        """The argument names that cost this fleet days, banned by name.

        A namespace, a selector, a uid or a timestamp is a value the model has to
        supply and cannot know, and every one of them has been supplied wrong.
        `find_object` exists precisely so a namespace is *returned* rather than
        asked for.
        """
        from mcp_runner.__main__ import build_registry

        forbidden = {
            "datasourceuid",
            "endtime",
            "starttime",
            "namespace",
            "labelselector",
            "fieldselector",
            "chatid",
        }
        registry = build_registry("homelab_facts")
        for name, tool in registry.tools.items():
            for argument in tool.schema.get("properties") or {}:
                assert argument.lower() not in forbidden, f"{name}.{argument}"

"""The Loki client - the second module that owns a query language.

Every rule in the oracle's prompt about Loki is asserted here instead: the
selector is built from resolved values, the window is resolved to nanoseconds
rather than written as `now-1h` (which this API rejects), and a failed query is
never an empty one.
"""

from __future__ import annotations

import httpx
import pytest

from mcp_runner.loki import ERROR_FILTER, Loki, LokiError, contains_filter, stream_selector


class TestTheSelectorIsBuilt:
    def test_namespace_and_pod(self):
        assert stream_selector("monitoring", "grafana-0") == '{namespace="monitoring",pod="grafana-0"}'

    def test_container_is_added_only_when_known(self):
        assert stream_selector("a", "b") == '{namespace="a",pod="b"}'
        assert stream_selector("a", "b", "c") == '{namespace="a",pod="b",container="c"}'

    def test_a_namespace_alone_is_a_valid_stream(self):
        """What a deleted pod leaves behind, and a real answer rather than a
        fishing expression."""
        assert stream_selector("automation") == '{namespace="automation"}'

    def test_no_wildcard_is_ever_produced(self):
        assert "=~" not in stream_selector("a", "b", "c")

    def test_quotes_in_a_value_are_escaped(self):
        assert stream_selector('a"b') == '{namespace="a\\"b"}'

    def test_contains_is_a_literal_filter_not_a_regex(self):
        """`|=` and not `|~`: any string is valid, so there is no bad regex to
        turn a filter into a query error."""
        assert contains_filter("a.b*") == '|= "a.b*"'

    def test_the_error_filter_is_case_insensitive_and_owned_here(self):
        assert ERROR_FILTER.startswith('|~ "(?i)')


class TestTheWindowIsResolved:
    def test_start_and_end_are_nanosecond_epochs(self, respx_mock):
        captured = {}

        def handler(request):
            captured.update(dict(request.url.params))
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})

        respx_mock.get("http://loki.test/loki/api/v1/query_range").mock(side_effect=handler)
        Loki(url="http://loki.test").query_range("{namespace=\"a\"}", since_seconds=3600)

        assert captured["start"].isdigit() and captured["end"].isdigit()
        # Nanoseconds: 19 digits for any date this decade, and never `now-1h`,
        # which Prometheus and Loki both reject as a parse error.
        assert len(captured["end"]) == 19
        assert int(captured["end"]) - int(captured["start"]) == 3600 * 10**9

    def test_the_read_is_bounded_and_backward(self, respx_mock):
        captured = {}
        respx_mock.get("http://loki.test/loki/api/v1/query_range").mock(
            side_effect=lambda request: (
                captured.update(dict(request.url.params)),
                httpx.Response(200, json={"status": "success", "data": {"result": []}}),
            )[1]
        )
        Loki(url="http://loki.test").query_range("{}", limit=7)
        assert captured["limit"] == "7"
        assert captured["direction"] == "backward"


class TestAFailedQueryIsNotAnEmptyOne:
    def test_an_http_failure_raises(self, respx_mock):
        respx_mock.get("http://loki.test/loki/api/v1/query_range").mock(
            return_value=httpx.Response(500, text="boom")
        )
        with pytest.raises(LokiError):
            Loki(url="http://loki.test").query_range("{}")

    def test_a_non_success_status_raises(self, respx_mock):
        respx_mock.get("http://loki.test/loki/api/v1/query_range").mock(
            return_value=httpx.Response(200, json={"status": "error"})
        )
        with pytest.raises(LokiError):
            Loki(url="http://loki.test").query_range("{}")

    def test_no_stream_is_an_empty_list_which_is_a_real_answer(self, respx_mock):
        respx_mock.get("http://loki.test/loki/api/v1/query_range").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": {"result": []}})
        )
        assert Loki(url="http://loki.test").query_range("{}") == []


class TestStreamsAreMerged:
    def test_entries_from_several_streams_come_back_newest_first(self, respx_mock):
        payload = {
            "status": "success",
            "data": {
                "result": [
                    {"stream": {"pod": "a"}, "values": [["100", "older"], ["300", "newest"]]},
                    {"stream": {"pod": "b"}, "values": [["200", "middle"]]},
                ]
            },
        }
        respx_mock.get("http://loki.test/loki/api/v1/query_range").mock(
            return_value=httpx.Response(200, json=payload)
        )
        entries = Loki(url="http://loki.test").query_range("{}")
        # Loki orders within a stream and not across them, so a caller reading
        # them in payload order gets an interleaved mess presented as a tail.
        assert [entry.line for entry in entries] == ["newest", "middle", "older"]

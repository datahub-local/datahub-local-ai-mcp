"""The byte budget, which is a hard requirement rather than a nicety.

A single ~16 KB tool result reproducibly ended a run with no report at all under
a 4B model: four calls for 24,126 result bytes produced `terminal turn had empty
text`, where five calls for 8,483 bytes the same day wrote a normal report. That
model is no longer in use, so the working size is now `MCP_BUDGET_BYTES` and the
per-tool budgets scale with it - but an answer still has to be bounded in code.
"""

from __future__ import annotations

import pytest

from mcp_runner.budget import DEFAULT_BUDGET_BYTES, clamp, truncate_lines
from mcp_runner.config import (
    BUDGET_REFERENCE_BYTES,
    ConfigError,
    scaled_budget,
    tool_budget,
    tool_cap,
)


def test_scaled_widens_every_tool_budget_by_the_same_factor():
    # The relative shape of the hand-tuned budgets survives a change of size.
    assert scaled_budget(BUDGET_REFERENCE_BYTES) == DEFAULT_BUDGET_BYTES
    assert scaled_budget(1024) == DEFAULT_BUDGET_BYTES // 4


def test_a_tool_budget_defaults_to_the_scaled_hand_tuned_value():
    assert tool_budget("logs") == scaled_budget(4096)
    assert tool_budget("explain") == scaled_budget(2048)


def test_a_tool_budget_can_be_overridden_by_its_own_env(monkeypatch):
    # Named after the registered tool, and absolute: what it asks for is what the
    # tool gets, whatever the global working size is.
    monkeypatch.setenv("MCP_BUDGET_LOGS", "9999")
    assert tool_budget("logs") == 9999
    assert tool_budget("why_failed") == scaled_budget(4096)


def test_caps_grow_with_the_budget_so_a_wider_answer_is_not_left_capped():
    # A wider byte budget is useless if the tool still gathers the old small list.
    assert tool_cap("series") == scaled_budget(40)
    assert tool_cap("limit") == scaled_budget(1000)


def test_a_cap_can_be_overridden_by_its_own_env(monkeypatch):
    monkeypatch.setenv("MCP_MAX_SERIES", "7")
    assert tool_cap("series") == 7


def test_an_unknown_tool_says_where_to_add_its_default():
    with pytest.raises(ConfigError, match="add it to config.py"):
        tool_budget("no_such_tool")


def test_short_input_is_returned_whole():
    lines = ["alpha", "beta", "gamma"]
    assert truncate_lines(lines, 4096) == "alpha\nbeta\ngamma"


def test_oversized_input_is_capped_at_the_budget():
    lines = [f"row {index} " + "x" * 200 for index in range(500)]
    out = truncate_lines(lines, 2048)
    assert len(out.encode()) <= 2048


def test_truncation_is_announced_with_a_count():
    lines = [f"row {index} " + "x" * 200 for index in range(100)]
    out = truncate_lines(lines, 1024, unit="claims")
    assert "TRUNCATED" in out
    # Silence is the failure mode: a report built on a quietly-cut answer reads
    # as complete, which is the invented-number problem in a new place.
    assert "claims not shown" in out


def test_truncation_never_splits_a_line():
    # Half a row of a table is a number without its label, which is exactly the
    # shape a model misreads.
    lines = [f"node-{index}  42.0%  ok" for index in range(200)]
    out = truncate_lines(lines, 512)
    for line in out.splitlines():
        assert line.startswith(("node-", "... TRUNCATED"))


def test_clamp_does_not_split_a_multibyte_character():
    # `status.result` is dropped outright on invalid UTF-8 - protobuf refuses to
    # marshal a bad string and the run still reports Succeeded with no error - so
    # a cut mid-character turns a large answer into a silently missing one.
    text = "é" * 4000
    out = clamp(text, 512)
    out.encode("utf-8").decode("utf-8")
    assert len(out.encode()) <= 512


def test_clamp_leaves_small_text_untouched():
    assert clamp("fine", 4096) == "fine"

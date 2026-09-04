"""The byte budget, which is a hard requirement rather than a nicety.

A single ~16 KB tool result reproducibly ends a run with no report at all: four
calls for 24,126 result bytes produced `terminal turn had empty text`, where five
calls for 8,483 bytes the same day wrote a normal report. Cumulative input was
25,423 tokens against a 65,536 window, so this is not context overflow - a 4B
model stops producing a final turn when one answer is that large.
"""

from __future__ import annotations

from mcp_runner.budget import DEFAULT_BUDGET_BYTES, clamp, truncate_lines


def test_default_budget_is_well_under_the_size_that_kills_a_run():
    # 16 KB killed a run; 8,483 bytes did not. The default must sit below both.
    assert DEFAULT_BUDGET_BYTES < 8483


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

"""Byte budgets for tool results.

A "fat tool" means *few calls*, never *big answers*: one oversized result ends a
run with no report at all, and not by overflowing the context. Each tool declares
a budget, truncates by whole lines, and says that it truncated. See ../README.md.
"""

from __future__ import annotations

# Per tool, not per run: well under the size that reproducibly kills a final
# turn, and above the largest result that worked.
DEFAULT_BUDGET_BYTES = 4096

# What a truncation notice costs, so the notice itself can never push a result
# back over its budget.
_NOTICE_RESERVE = 160


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def truncate_lines(
    lines: list[str],
    budget: int = DEFAULT_BUDGET_BYTES,
    *,
    unit: str = "rows",
) -> str:
    """Join ``lines``, dropping whole lines to fit ``budget``.

    By line and never mid-line: half a row is a number without its label. A drop
    is announced with a count, so a partial answer cannot read as complete.
    """
    kept: list[str] = []
    used = 0
    limit = budget - _NOTICE_RESERVE

    for index, line in enumerate(lines):
        cost = _byte_len(line) + 1
        if used + cost > limit and index < len(lines):
            dropped = len(lines) - len(kept)
            kept.append(f"... TRUNCATED: {dropped} more {unit} not shown (byte budget {budget}).")
            return "\n".join(kept)
        kept.append(line)
        used += cost

    return "\n".join(kept)


def clamp(text: str, budget: int = DEFAULT_BUDGET_BYTES) -> str:
    """Last-resort guard for text that is not line-structured.

    Cuts on a UTF-8 character boundary: a reply carrying invalid UTF-8 has its
    result dropped entirely while the run still reports success.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    head = encoded[: budget - _NOTICE_RESERVE].decode("utf-8", errors="ignore")
    return f"{head}\n... TRUNCATED at {budget} bytes."

"""Text shaping for tool results.

Compact tables rather than JSON: a small model copies rows out of a table and
re-derives values from nested fields. This is also where ages become facts - the
server has a clock, so it does the subtraction and returns a duration the model
can report without one. Output is ASCII-only. See ../README.md.
"""

from __future__ import annotations

import datetime as _datetime
import logging

from .prometheus import UNAVAILABLE

logger = logging.getLogger(__name__)

# Only ASCII in rendered output: a reply carrying invalid UTF-8 has its result
# dropped entirely while the run still reports success.
_ASCII_FALLBACK = {"·": "-", "—": "-", "–": "-", "→": "->", "°": ""}


def ascii_only(text: str) -> str:
    """Fold known non-ASCII to ASCII and drop the rest."""
    for source, replacement in _ASCII_FALLBACK.items():
        text = text.replace(source, replacement)
    return text.encode("ascii", errors="ignore").decode("ascii")


def number(value: float | None, digits: int = 1, suffix: str = "") -> str:
    """Format a metric, or the `unavailable` literal when there is none.

    `unavailable` means the query answered nothing. Never `0`, never an estimate,
    and never a value borrowed from a different metric.
    """
    if value is None:
        return UNAVAILABLE
    return f"{value:.{digits}f}{suffix}"


def bytes_human(value: float | None) -> str:
    if value is None:
        return UNAVAILABLE
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TiB"


def parse_timestamp(value: str | None) -> _datetime.datetime | None:
    """Parse a Kubernetes RFC3339 timestamp. Unparseable is ``None``."""
    if not value or not isinstance(value, str):
        return None
    try:
        return _datetime.datetime.fromisoformat(value)
    except ValueError:
        logger.warning("unparseable timestamp %r", value)
        return None


def age(value: str | None, *, now: _datetime.datetime | None = None) -> str:
    """Render how long ago ``value`` was, as ``3.2d ago`` / ``in 14.0d``.

    The direction is spelled out rather than signed: "expires in 14 days" and
    "expired 14 days ago" are opposite findings, and a leading `-` is easy to
    drop.
    """
    stamp = parse_timestamp(value)
    if stamp is None:
        return UNAVAILABLE
    reference = now or _datetime.datetime.now(_datetime.UTC)
    delta = (reference - stamp).total_seconds()
    if abs(delta) < 3600:
        return f"{abs(delta) / 60:.0f}m {'ago' if delta >= 0 else 'from now'}"
    days = abs(delta) / 86400
    if days < 1:
        return f"{abs(delta) / 3600:.1f}h {'ago' if delta >= 0 else 'from now'}"
    return f"{days:.1f}d ago" if delta >= 0 else f"in {days:.1f}d"


def days_until(value: str | None, *, now: _datetime.datetime | None = None) -> float | None:
    """Signed days until ``value``; negative is in the past. ``None`` if unparseable."""
    stamp = parse_timestamp(value)
    if stamp is None:
        return None
    reference = now or _datetime.datetime.now(_datetime.UTC)
    return (stamp - reference).total_seconds() / 86400


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Render a fixed-width table as lines, so the byte budget can drop whole
    rows rather than splitting one."""
    if not rows:
        return ["(no rows)"]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row[: len(widths)]):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(padded)).rstrip())
    return lines

"""Snapshot persistence, so a trend is measured rather than asserted.

A lost snapshot degrades a diff to "first observation", which every tool states
explicitly; it can never produce a *wrong* diff. See ../README.md.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)


class Snapshots:
    def __init__(self, directory: str | None = None) -> None:
        self.directory = Path(directory or config.state_dir())

    def _path(self, key: str) -> Path:
        safe = "".join(char for char in key if char.isalnum() or char in "-_")
        return self.directory / f"{safe}.json"

    def load(self, key: str) -> dict[str, Any] | None:
        """Return the stored snapshot, or ``None`` if there is not a usable one.

        Any read or parse failure is `None`: the tool's primary reading is still
        good, and losing it to a cache problem would be the worse outcome.
        """
        path = self._path(key)
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("discarding unreadable snapshot %s: %s", path, exc)
            return None

    def save(self, key: str, payload: dict[str, Any]) -> None:
        """Write ``payload`` atomically. A failure is logged, never raised."""
        path = self._path(key)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", dir=self.directory, delete=False, encoding="utf-8"
            ) as handle:
                json.dump(payload, handle)
                temporary = handle.name
            os.replace(temporary, path)
        except OSError as exc:
            logger.warning("could not persist snapshot %s: %s", path, exc)


def diff_keys(
    current: set[str], previous: set[str] | None
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(new, continuing, resolved)`` sorted, or all-new on a first run.

    ``previous is None`` means no prior observation, and the caller says so -
    unqualified "new" would read as a fleet-wide incident after a restart.
    """
    if previous is None:
        return sorted(current), [], []
    return (
        sorted(current - previous),
        sorted(current & previous),
        sorted(previous - current),
    )

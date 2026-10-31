"""Fixtures for the OpenCode shim: an env that points at a fake server."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def opencode_env(monkeypatch):
    monkeypatch.setenv("OPENCODE_SERVER_URL", "http://opencode.test")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "secret")
    monkeypatch.setenv("OPENCODE_WORKSPACE_ROOT", "/workspace")

"""The render proxy: it hands back a reference, and it fails readably.

The properties worth guarding are the reference shape (not the bytes), that the
request carries a spec and the store flag and nothing else, and that a missing
RENDER_URL names the variable rather than reporting a broken renderer.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from render.tools import render

from mcp_runner.config import ConfigError

BASE = "http://render.internal:8080"

SPEC = {"title": "Homelab data flow", "blocks": [{"label": "A", "value": "1"}], "accent": "#22d3ee"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("RENDER_URL", BASE)
    monkeypatch.delenv("RENDER_TIMEOUT_SECONDS", raising=False)


def _ok_response():
    return httpx.Response(
        200,
        json={
            "id": "a" * 32,
            "url": f"/files/{'a' * 32}.mp4",
            "bytes": 355846,
            "durationMs": 4000,
            "frames": 120,
            "width": 1080,
            "height": 1350,
            "fps": "30/1",
            "codec": "h264",
        },
    )


class TestRenderAsset:
    def test_returns_a_reference_not_bytes(self):
        with respx.mock:
            route = respx.post(f"{BASE}/render").mock(return_value=_ok_response())
            out = render.render_asset(json.dumps(SPEC))

        assert "rendered mp4" in out
        assert f"id: {'a' * 32}" in out
        assert f"url: {BASE}/files/{'a' * 32}.mp4" in out
        assert "duration: 4.00s (120 frames" in out
        assert "size: 1080x1350" in out
        assert route.called

    def test_sends_a_spec_and_the_store_flag(self):
        with respx.mock:
            route = respx.post(f"{BASE}/render").mock(return_value=_ok_response())
            render.render_asset(json.dumps(SPEC))

        body = json.loads(route.calls[0].request.content)
        assert body["store"] is True
        assert body["spec"] == SPEC
        # Nothing executable crosses the boundary.
        assert set(body) == {"spec", "store"}

    def test_invalid_json_is_reported_not_raised(self):
        with respx.mock:
            out = render.render_asset("{not json")
        assert out.startswith("INVALID")

    def test_service_error_is_surfaced(self):
        with respx.mock:
            respx.post(f"{BASE}/render").mock(
                return_value=httpx.Response(400, json={"error": "unknown layout: pie"})
            )
            out = render.render_asset(json.dumps(SPEC))
        assert out == "RENDER FAILED: unknown layout: pie"

    def test_unreachable_service_is_distinct_from_a_rejection(self):
        with respx.mock:
            respx.post(f"{BASE}/render").mock(side_effect=httpx.ConnectError("refused"))
            out = render.render_asset(json.dumps(SPEC))
        assert out.startswith("RENDER SERVICE ERROR")

    def test_missing_url_names_the_variable(self, monkeypatch):
        monkeypatch.delenv("RENDER_URL", raising=False)
        with pytest.raises(ConfigError) as exc:
            render.render_asset(json.dumps(SPEC))
        assert "RENDER_URL" in str(exc.value)

    def test_within_budget(self):
        with respx.mock:
            respx.post(f"{BASE}/render").mock(return_value=_ok_response())
            out = render.render_asset(json.dumps(SPEC))
        assert len(out.encode()) <= render.RENDER_BUDGET

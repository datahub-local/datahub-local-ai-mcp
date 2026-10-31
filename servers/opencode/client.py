"""The OpenCode v2 HTTP client, reduced to the calls the shim makes.

Every method returns the `data` member of the API envelope, or `None` for a 204.
A 404 on a session is `None` rather than an exception because the caller decides
whether a missing session is an error (`get_session`) or a resume that must create
one (`run_task`). A 409 is `SessionBusy`: the API answers it when a turn is already
running, and the shim has to report whose turn it is.
"""

from __future__ import annotations

from typing import Self

import httpx

from . import settings


class OpenCodeError(RuntimeError):
    """The server answered, but not with what the call needs."""


class SessionBusy(OpenCodeError):
    def __init__(self, session_id: str, message: str = "session is busy"):
        super().__init__(message)
        self.session_id = session_id


class OpenCodeClient:
    def __init__(
        self,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
    ):
        self._client = httpx.Client(
            base_url=url or settings.server_url(),
            auth=(username or settings.username(), password or settings.password()),
            timeout=timeout or settings.request_timeout(),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs):
        response = self._client.request(method, path, timeout=timeout, **kwargs)
        if response.status_code == 401:
            raise OpenCodeError(
                "the OpenCode server rejected the shim's Basic credential "
                "(OPENCODE_SERVER_USERNAME / OPENCODE_SERVER_PASSWORD)"
            )
        return response

    def _data(self, response: httpx.Response):
        if response.status_code == 204 or not response.content:
            return None
        return response.json().get("data")

    def _fail(self, response: httpx.Response, session_id: str | None = None) -> None:
        body = response.text[:300]
        if response.status_code == 404 and session_id:
            raise KeyError(session_id)
        if response.status_code == 409:
            payload = {}
            try:
                payload = response.json()
            except ValueError:
                pass
            busy = payload.get("sessionID") or session_id or "unknown"
            raise SessionBusy(busy, payload.get("message") or "session is busy")
        raise OpenCodeError(f"OpenCode API answered {response.status_code}: {body}")

    # -- sessions ---------------------------------------------------------

    def create_session(self, directory: str | None = None) -> dict:
        body = {"location": {"directory": directory}} if directory else {}
        response = self._request("POST", "/api/session", json=body)
        if response.status_code >= 400:
            self._fail(response)
        return self._data(response)

    def get_session(self, session_id: str) -> dict | None:
        response = self._request("GET", f"/api/session/{session_id}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            self._fail(response)
        return self._data(response)

    def active(self) -> dict:
        response = self._request("GET", "/api/session/active")
        if response.status_code >= 400:
            self._fail(response)
        return self._data(response) or {}

    def prompt(self, session_id: str, text: str, timeout: float | None = None) -> dict:
        response = self._request(
            "POST",
            f"/api/session/{session_id}/prompt",
            timeout=timeout,
            json={"text": text},
        )
        if response.status_code >= 400:
            self._fail(response, session_id)
        return self._data(response)

    def wait(self, session_id: str, timeout: float) -> bool:
        """Block until the agent loop is idle. `False` means the deadline passed."""
        try:
            response = self._request(
                "POST", f"/api/experimental/session/{session_id}/wait", timeout=timeout
            )
        except httpx.TimeoutException:
            return False
        # 503 is "no loop to wait on", which is the idle state as far as a caller is
        # concerned; the session read that follows decides the reported status.
        if response.status_code == 503:
            return True
        if response.status_code >= 400:
            self._fail(response, session_id)
        return True

    def interrupt(self, session_id: str) -> bool:
        response = self._request("POST", f"/api/session/{session_id}/interrupt")
        if response.status_code == 404:
            raise KeyError(session_id)
        if response.status_code >= 400:
            self._fail(response)
        return bool((response.json() or {}).get("interrupted"))

    def messages(self, session_id: str) -> list[dict]:
        response = self._request("GET", f"/api/session/{session_id}/message")
        if response.status_code >= 400:
            self._fail(response, session_id)
        return self._data(response) or []

    def session_list(self, limit: int = 10, search: str | None = None) -> list[dict]:
        params: dict[str, str] = {"limit": str(limit), "order": "desc"}
        if search:
            params["search"] = search
        response = self._request("GET", "/api/session", params=params)
        if response.status_code >= 400:
            self._fail(response)
        return self._data(response) or []

    def transcript(self, session_id: str, limit: int = 20) -> list[dict]:
        """The newest `limit` messages, returned oldest-first for reading."""
        response = self._request(
            "GET",
            f"/api/session/{session_id}/message",
            params={"limit": str(limit), "order": "desc"},
        )
        if response.status_code >= 400:
            self._fail(response, session_id)
        return list(reversed(self._data(response) or []))

    def diff(
        self,
        session_id: str,
        *,
        from_message: str | None = None,
        context: int | None = None,
    ) -> list[dict]:
        params: dict[str, str] = {}
        if from_message:
            params["from"] = from_message
        if context is not None:
            params["context"] = str(context)
        response = self._request("GET", f"/api/session/{session_id}/diff", params=params or None)
        if response.status_code >= 400:
            self._fail(response, session_id)
        return self._data(response) or []

    def forms(self, session_id: str) -> list[dict]:
        response = self._request("GET", f"/api/session/{session_id}/form")
        if response.status_code >= 400:
            self._fail(response, session_id)
        return self._data(response) or []

    def permissions(self, session_id: str) -> list[dict]:
        response = self._request("GET", f"/api/session/{session_id}/permission")
        if response.status_code >= 400:
            self._fail(response, session_id)
        return self._data(response) or []

    # -- input ------------------------------------------------------------

    def reply_form(self, session_id: str, form_id: str, answer: dict) -> None:
        response = self._request(
            "POST",
            f"/api/session/{session_id}/form/{form_id}/reply",
            json={"answer": answer},
        )
        if response.status_code >= 400:
            self._fail(response, session_id)

    def reply_permission(self, session_id: str, request_id: str, decision: str) -> None:
        response = self._request(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            json={"decision": decision},
        )
        if response.status_code >= 400:
            self._fail(response, session_id)

"""A session in the four states the shim reports, and how they are rendered.

The API has no single "what is this session doing" answer: activity is a separate
endpoint, questions and permission prompts are separate lists, and the outcome of
the last turn is on the session or on its idle message. This module is the one
place that folds those into `running | idle | needs_input | error`.

Order matters. Pending input wins over activity, because a turn blocked on a
question is "running" in `/api/session/active` and useless to report that way: the
caller has to answer it. Activity beats the stored outcome, because a resumed
session still carries the previous turn's outcome until the new one finishes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import OpenCodeClient


@dataclass
class Status:
    session_id: str
    state: str
    text: str = ""
    outcome: str = ""
    error: str = ""
    files: list[dict] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)
    permissions: list[dict] = field(default_factory=list)


def _text_of(message: dict) -> str:
    parts = message.get("content") or []
    return "\n".join(
        part.get("text", "") for part in parts if part.get("type") == "text" and part.get("text")
    ).strip()


def last_text(messages: list[dict]) -> str:
    """The most recent assistant text, not every message concatenated.

    A session can run for days; the caller wants the answer to the turn it just
    started, and the full transcript is what the web UI is for.
    """
    for message in reversed(messages):
        if message.get("type") != "assistant":
            continue
        text = _text_of(message)
        if text:
            return text
    return ""


def _idle_message(messages: list[dict]) -> dict | None:
    for message in reversed(messages):
        if message.get("type") == "idle":
            return message
    return None


def _error_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        error = message.get("error") or {}
        if error.get("message"):
            return error["message"]
    for message in reversed(messages):
        if message.get("type") != "assistant":
            continue
        for part in message.get("content") or []:
            state = part.get("state") or {}
            if part.get("type") == "tool" and state.get("status") == "error":
                return f"tool {part.get('name')} failed: {state.get('error') or 'unknown error'}"
    return ""


def read(client: OpenCodeClient, session_id: str) -> Status:
    forms = client.forms(session_id)
    permissions = client.permissions(session_id)
    messages = client.messages(session_id)
    status = Status(
        session_id=session_id,
        state="idle",
        text=last_text(messages),
        forms=forms,
        permissions=permissions,
    )

    if forms or permissions:
        status.state = "needs_input"
        return status

    if session_id in client.active():
        status.state = "running"
        return status

    session = client.get_session(session_id)
    if session is None:
        raise KeyError(session_id)

    idle = _idle_message(messages) or {}
    status.outcome = session.get("outcome") or idle.get("outcome") or ""
    if status.outcome == "failed":
        status.state = "error"
        status.error = _error_text(messages) or "the turn failed without a message"
        return status

    status.files = client.diff(session_id)
    return status


def working(client: OpenCodeClient) -> dict[str, str]:
    """Turns in progress, keyed by session, valued by their checkout directory.

    `/api/session/active` reports a session waiting for an answer as running, and
    it is idle in every way that matters here: no CPU, no progress, no end. If it
    consumed a concurrency slot, one question would stop the whole queue until the
    owner replied.

    The directory comes from the session itself, not from the caller, because two
    turns in one checkout is the one state that must never be allowed: both would
    write the same files.
    """
    busy: dict[str, str] = {}
    for session_id in client.active() or {}:
        if client.forms(session_id) or client.permissions(session_id):
            continue
        session = client.get_session(session_id) or {}
        busy[session_id] = ((session.get("location") or {}).get("directory") or "").rstrip("/")
    return busy


def render(status: Status) -> str:
    """Compact, line-oriented text: a model copies rows, it does not parse prose."""
    lines = [f"session: {status.session_id}", f"status: {status.state}"]
    if status.outcome:
        lines.append(f"outcome: {status.outcome}")
    if status.error:
        lines.append(f"error: {status.error}")
    if status.text:
        lines.append("text:")
        lines.extend(f"  {line}" for line in status.text.splitlines())
    for form in status.forms:
        lines.append(f"form: {form.get('id')}")
        lines.append(f"question: {form.get('title') or '(untitled)'}")
        for field_spec in form.get("fields") or []:
            detail = field_spec.get("type") or "string"
            options = [option.get("value") for option in field_spec.get("options") or []]
            if options:
                detail += f": {' | '.join(str(option) for option in options)}"
            required = " required" if field_spec.get("required") else ""
            lines.append(f"  field: {field_spec.get('key')} ({detail}){required}")
    for request in status.permissions:
        lines.append(f"permission: {request.get('id')}")
        lines.append(
            f"  action: {request.get('action')} on {', '.join(request.get('resources') or [])}"
        )
        if request.get("message"):
            lines.append(f"  reason: {request.get('message')}")
    if status.files:
        lines.append("files:")
        for change in status.files:
            lines.append(
                f"  {change.get('status', '?')[:1].upper()} {change.get('file')} "
                f"(+{change.get('additions', 0)} -{change.get('deletions', 0)})"
            )
    if status.state == "needs_input":
        lines.append(
            "answer with respond: form_id/request_id above, answer as a JSON object "
            "keyed by field (or the decision once|always|reject for a permission)"
        )
    return "\n".join(lines)


def render_busy(active: dict, requested: str | None = None, limit: int | None = None) -> str:
    session_id = requested if requested in active else next(iter(active), "unknown")
    slots = f" ({len(active)} of {limit} slots in use)" if limit else ""
    return "\n".join(
        [
            f"session: {session_id}",
            "status: busy",
            f"another turn is running{slots}; poll get_session, or abort_session to stop it",
        ]
    )

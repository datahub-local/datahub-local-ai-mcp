"""The four tools Hermes and Sympozium call, and what they refuse to guess.

`run_task` waits a bounded time, not for the turn: a session may run for days, and
the tool contract is `running` plus a session id rather than a blocked MCP call.
`directory` is validated against the workspace root here even though the shim
cannot see that filesystem — the API would accept any absolute path, including
`/etc`, and this is the only place that check can happen.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path

from . import settings
from .client import OpenCodeClient, OpenCodeError, SessionBusy
from .state import read, render, render_busy, working

RUN_TASK_DESCRIPTION = """
Start a task in the OpenCode agent and wait up to `wait_seconds` for it to finish.
Returns `running` with the session id when the turn outlives the wait; the session
keeps running, and `get_session` returns the result later. Returns `busy` when the
configured number of turns is already running, or when this session is one of them.
A session parked on a question does not count, so asking the owner never stops the
queue. Two turns in the same directory are always refused, whatever the limit. So a
task that should run alongside another needs its own `directory`, relative to
`/workspace`; omit it to work in the workspace root. Pass `session_id` to
continue an idle session instead of starting a new one.
"""

RUN_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "the task, as one message"},
        "directory": {
            "type": "string",
            "description": "workspace-relative directory for a new session",
        },
        "session_id": {
            "type": "string",
            "description": "continue this idle session instead of creating one",
        },
        "wait_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": settings.MAX_WAIT_SECONDS,
            "description": "how long to wait for the turn before returning running",
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

GET_SESSION_DESCRIPTION = """
Report a session without waiting: `running`, `idle`, `needs_input` (with the form or
permission prompt), or `error`, plus the latest assistant text and, when idle, the
files the turn changed. Use it to poll a task that returned `running`.
"""

GET_SESSION_SCHEMA = {
    "type": "object",
    "properties": {"session_id": {"type": "string", "description": "session to report"}},
    "required": ["session_id"],
    "additionalProperties": False,
}

RESPOND_DESCRIPTION = """
Answer the question or permission request `get_session` reported. For a form, pass
`form_id` and `answer`: a JSON object keyed by field, or a plain value when the form
has one field. For a permission request, pass `request_id` and `answer` as one of
`once`, `always`, or `reject`.
"""

RESPOND_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "session holding the prompt"},
        "form_id": {"type": "string", "description": "form to answer"},
        "request_id": {"type": "string", "description": "permission request to answer"},
        "answer": {"type": "string", "description": "value, JSON object, or decision"},
    },
    "required": ["session_id", "answer"],
    "additionalProperties": False,
}

ABORT_SESSION_DESCRIPTION = """
Interrupt the running turn in a session. The session and its history survive; only
the current turn stops. Reports whether an active turn was actually interrupted.
"""

ABORT_SESSION_SCHEMA = {
    "type": "object",
    "properties": {"session_id": {"type": "string", "description": "session to interrupt"}},
    "required": ["session_id"],
    "additionalProperties": False,
}


GET_DIFF_DESCRIPTION = """
The patch text of what a session changed, per file, with additions and deletions.
Use it to review before opening or merging a pull request. `from_message` narrows
the diff to the turn of that user message; `context` sets unchanged lines around
each hunk (omit for full-file patches). Output is bounded like every tool's.
"""

GET_DIFF_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "session to review"},
        "from_message": {
            "type": "string",
            "description": "user message whose turn to diff; default the newest turn",
        },
        "context": {
            "type": "integer",
            "minimum": 0,
            "description": "unchanged lines around each hunk",
        },
    },
    "required": ["session_id"],
    "additionalProperties": False,
}

LIST_SESSIONS_DESCRIPTION = """
The newest sessions, with id, state and title. Use it to recover a session id after
a restart, or `search` to find one by title or content. The shim keeps no list of
its own; this is the server's.
"""

LIST_SESSIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "how many"},
        "search": {"type": "string", "description": "substring filter over title and messages"},
    },
    "additionalProperties": False,
}

GET_TRANSCRIPT_DESCRIPTION = """
The newest messages of a session, oldest first: user text, assistant text, tool
calls and their state, shell commands, and the outcome line. Use it when the last
answer is not enough, for example to see which command failed. Bounded by `limit`.
"""

GET_TRANSCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "session to read"},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "newest messages to return",
        },
    },
    "required": ["session_id"],
    "additionalProperties": False,
}


def _directory(value: str | None) -> str | None:
    if not value:
        return None
    root = Path(settings.workspace_root()).resolve()
    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError(f"directory must be workspace-relative, not absolute: {value}")
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"directory escapes the workspace: {value}")
    return str(resolved)


def _answer(field_spec: dict, raw: str):
    kind = field_spec.get("type")
    if kind == "boolean":
        return raw.strip().lower() in {"true", "yes", "1", "on"}
    if kind == "number":
        return float(raw)
    if kind == "multiselect":
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


def _form_answer(fields: list[dict], raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"answer is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("answer JSON must be an object keyed by field")
        return parsed
    if len(fields) != 1:
        keys = ", ".join(str(field.get("key")) for field in fields)
        raise ValueError(f"this form has several fields ({keys}); answer with a JSON object")
    field_spec = fields[0]
    return {field_spec.get("key"): _answer(field_spec, raw)}


def run_task(
    prompt: str,
    directory: str | None = None,
    session_id: str | None = None,
    wait_seconds: int | None = None,
) -> str:
    wait = settings.wait_seconds(wait_seconds)
    deadline = time.monotonic() + wait

    try:
        target = _directory(directory)
    except ValueError as exc:
        return f"ERROR: {exc}"

    with OpenCodeClient() as client:
        try:
            running = working(client)
        except OpenCodeError as exc:
            return f"ERROR: {exc}"

        limit = settings.max_concurrent()
        resumed = None
        if session_id:
            if session_id in running:
                return render_busy({session_id: running[session_id]}, session_id, limit)
            try:
                resumed = client.get_session(session_id)
            except OpenCodeError as exc:
                return f"ERROR: {exc}"
            if resumed is None:
                return f"ERROR: no session {session_id}"
            # Resuming must not skip the one rule that keeps checkouts intact: the
            # directory comes from the session, not from the caller, because the
            # caller does not pass one when it continues a session.
            checkout = ((resumed.get("location") or {}).get("directory") or "").rstrip("/")
        else:
            checkout = (target or settings.workspace_root()).rstrip("/")

        clash = {sid: d for sid, d in running.items() if d and d == checkout}
        if clash:
            return render_busy(clash, session_id, limit)
        if len(running) >= limit:
            return render_busy(running, session_id, limit)

        try:
            if session_id:
                sid = session_id
            else:
                sid = client.create_session(directory=target)["id"]

            try:
                client.prompt(sid, prompt, timeout=wait)
            except SessionBusy as exc:
                return render_busy({exc.session_id: {"type": "running"}}, exc.session_id, limit)

            remaining = deadline - time.monotonic()
            if remaining > 0 and sid in client.active():
                client.wait(sid, timeout=remaining)
        except OpenCodeError as exc:
            return f"ERROR: {exc}"

        return render(read(client, sid))


def get_session(session_id: str) -> str:
    with OpenCodeClient() as client:
        try:
            return render(read(client, session_id))
        except KeyError:
            return f"ERROR: no session {session_id}"
        except OpenCodeError as exc:
            return f"ERROR: {exc}"


def respond(
    session_id: str,
    answer: str,
    form_id: str | None = None,
    request_id: str | None = None,
) -> str:
    if not form_id and not request_id:
        return "ERROR: pass form_id or request_id"

    with OpenCodeClient() as client:
        try:
            if request_id:
                decision = answer.strip().lower()
                if decision not in {"once", "always", "reject"}:
                    return "ERROR: a permission answer is once, always, or reject"
                client.reply_permission(session_id, request_id, decision)
            else:
                form = next(
                    (item for item in client.forms(session_id) if item.get("id") == form_id),
                    None,
                )
                if form is None:
                    return f"ERROR: no pending form {form_id} in {session_id}"
                try:
                    payload = _form_answer(form.get("fields") or [], answer)
                except ValueError as exc:
                    return f"ERROR: {exc}"
                client.reply_form(session_id, form_id, payload)
            return render(read(client, session_id))
        except KeyError:
            return f"ERROR: no session {session_id}"
        except OpenCodeError as exc:
            return f"ERROR: {exc}"


def abort_session(session_id: str) -> str:
    with OpenCodeClient() as client:
        try:
            interrupted = client.interrupt(session_id)
        except KeyError:
            return f"ERROR: no session {session_id}"
        except OpenCodeError as exc:
            return f"ERROR: {exc}"
        try:
            status = render(read(client, session_id))
        except KeyError:
            return f"ERROR: no session {session_id}"
        except OpenCodeError as exc:
            return f"ERROR: {exc}"
        return f"interrupted: {'true' if interrupted else 'false'}\n{status}"


def _prefixed(prefix: str, text: str) -> list[str]:
    lines = text.splitlines() or [""]
    out = [f"{prefix}: {lines[0]}"]
    out.extend(f"  {line}" for line in lines[1:])
    return out


def _message_lines(message: dict) -> list[str]:
    kind = message.get("type")
    if kind in {"user", "system"}:
        return _prefixed(kind, message.get("text") or "")
    if kind == "assistant":
        lines: list[str] = []
        text = "\n".join(
            part.get("text", "")
            for part in message.get("content") or []
            if part.get("type") == "text" and part.get("text")
        )
        if text:
            lines.extend(_prefixed("assistant", text))
        for part in message.get("content") or []:
            if part.get("type") != "tool":
                continue
            state = (part.get("state") or {}).get("status") or "unknown"
            lines.append(f"  tool: {part.get('name')} ({state})")
        return lines
    if kind == "shell":
        status = message.get("status") or "unknown"
        return _prefixed("shell", f"{message.get('command') or ''} [{status}]")
    if kind == "idle":
        return [f"idle: {message.get('outcome') or 'unknown'}"]
    return []


def get_diff(
    session_id: str,
    from_message: str | None = None,
    context: int | None = None,
) -> str:
    with OpenCodeClient() as client:
        try:
            changes = client.diff(session_id, from_message=from_message, context=context)
        except KeyError:
            return f"ERROR: no session {session_id}"
        except OpenCodeError as exc:
            return f"ERROR: {exc}"

    if not changes:
        return f"session: {session_id}\nchanges: none"
    lines = [f"session: {session_id}", f"files: {len(changes)}"]
    for change in changes:
        status = (change.get("status") or "?").upper()[:1]
        lines.append(
            f"{status} {change.get('file')} "
            f"(+{change.get('additions', 0)} -{change.get('deletions', 0)})"
        )
        lines.extend(f"  {line}" for line in (change.get("patch") or "").splitlines())
    return "\n".join(lines)


def list_sessions(limit: int | None = None, search: str | None = None) -> str:
    count = max(1, min(int(limit or 10), 50))
    with OpenCodeClient() as client:
        try:
            sessions = client.session_list(limit=count, search=search)
        except OpenCodeError as exc:
            return f"ERROR: {exc}"

    if not sessions:
        return "sessions: none"
    lines = [f"sessions: {len(sessions)}"]
    for session in sessions:
        updated = (session.get("time") or {}).get("updated")
        when = (
            _dt.datetime.fromtimestamp(updated / 1000, tz=_dt.UTC).strftime("%Y-%m-%d %H:%M")
            if updated
            else "unknown"
        )
        state = session.get("outcome") or "idle"
        lines.append(f"{session.get('id')}  {state}  {when}")
        if session.get("title"):
            lines.append(f"  {session['title']}")
    return "\n".join(lines)


def get_transcript(session_id: str, limit: int | None = None) -> str:
    count = max(1, min(int(limit or 20), 100))
    with OpenCodeClient() as client:
        try:
            messages = client.transcript(session_id, limit=count)
        except KeyError:
            return f"ERROR: no session {session_id}"
        except OpenCodeError as exc:
            return f"ERROR: {exc}"

    lines = [f"session: {session_id}", f"messages: {len(messages)}"]
    for message in messages:
        lines.extend(_message_lines(message))
    return "\n".join(lines)

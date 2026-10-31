# opencode

Seven tools that let Hermes and Sympozium personas drive the OpenCode coding agent:

| Tool             | What it does                                                         |
| ---------------- | -------------------------------------------------------------------- |
| `run_task`       | Start a turn and wait a bounded time; returns `running` if it lasts  |
| `get_session`    | Report a session without waiting                                     |
| `get_diff`       | The patch text a session produced, per file                          |
| `list_sessions`  | Newest sessions, searchable — how a stateless shim finds one again   |
| `get_transcript` | The newest messages, oldest first, when the last answer is not enough |
| `respond`        | Answer a question form or a permission request                       |
| `abort_session`  | Interrupt the current turn, keeping the session                      |

Targets the **OpenCode v2** HTTP API (`/api/session*`). The upstream
[`opencode-mcp`](https://github.com/alaeddinemessadi/opencode-mcp) speaks OpenCode
1.x (v2 support planned for 4.0) and exposes 70 tools, so it is not usable here.

## Why the shim exists

OpenCode has no MCP server and Hermes is an MCP client. The shim is the smallest
thing between them, and it holds **no state of its own**:

- "One active session" is read from `GET /api/session/active`, never from memory, so
  a shim restart cannot start a second writer.
- A turn is unbounded. `run_task` waits at most `wait_seconds` (default 120, max
  300) and then returns `running` with the session id; the session keeps going and
  `get_session` returns the result later.
- Pending forms and permission requests are reported as `needs_input` with the ids
  and fields `respond` needs.
- `directory` is workspace-relative and validated here: the API accepts any
  absolute path, including `/etc`.

## Status

`needs_input` wins over `running`: a turn blocked on a question is active in the
API and useless to report that way. `running` wins over the stored outcome, because
a resumed session keeps the previous turn's outcome until the new one finishes.
Otherwise the session's outcome decides `idle` or `error`, and the diff is read
only for an idle session.

## Configuration

| Variable                          | Default                  | Purpose                          |
| --------------------------------- | ------------------------ | -------------------------------- |
| `OPENCODE_SERVER_URL`             | required                 | e.g. `http://…-server:4096`      |
| `OPENCODE_SERVER_USERNAME`        | `opencode`               | Basic auth user                  |
| `OPENCODE_SERVER_PASSWORD`        | required                 | Basic auth password              |
| `OPENCODE_WORKSPACE_ROOT`         | `/workspace`             | what `directory` is relative to  |
| `OPENCODE_REQUEST_TIMEOUT_SECONDS`| `20`                     | per HTTP call, not per turn      |

There is no default for the URL or the password: a guessed endpoint would report a
healthy agent while every call failed.

## Tests

`tests/opencode/` mocks the API with `respx` and covers the bounded wait, busy,
`needs_input` + `respond`, error, truncation, and a directory that escapes the
workspace.

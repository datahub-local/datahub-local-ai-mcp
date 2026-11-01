"""The shim's contract, asserted against a fake OpenCode API.

The properties worth guarding: a long turn returns `running` without ending the
session, a second task is refused before a session is created, pending input is
surfaced as `needs_input` with something `respond` can use, and a directory that
escapes `/workspace` is rejected before any request is sent.
"""

from __future__ import annotations

import json

import httpx
from opencode import tools

from mcp_runner.server import Registry


def _mock_idle_session(
    respx_mock, *, session_id="ses_x", text="done", files=None, outcome="succeeded"
):
    respx_mock.get("http://opencode.test/api/session/" + session_id + "/form").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get("http://opencode.test/api/session/" + session_id + "/permission").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get("http://opencode.test/api/session/" + session_id + "/message").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "msg_1",
                        "type": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                    {"id": "msg_2", "type": "idle", "outcome": outcome},
                ],
                "cursor": {"previous": None, "next": None},
            },
        )
    )
    respx_mock.get("http://opencode.test/api/session/active").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )
    respx_mock.get("http://opencode.test/api/session/" + session_id).mock(
        return_value=httpx.Response(200, json={"data": {"id": session_id, "outcome": outcome}})
    )
    respx_mock.get("http://opencode.test/api/session/" + session_id + "/diff").mock(
        return_value=httpx.Response(200, json={"data": files or []})
    )


def _mock_active(respx_mock, active, directories=None):
    """The active list, with no pending input and a checkout per session."""
    directories = directories or {}
    respx_mock.get("http://opencode.test/api/session/active").mock(
        return_value=httpx.Response(200, json={"data": active})
    )
    for session_id in active:
        respx_mock.get(f"http://opencode.test/api/session/{session_id}/form").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx_mock.get(f"http://opencode.test/api/session/{session_id}/permission").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx_mock.get(f"http://opencode.test/api/session/{session_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "id": session_id,
                        "location": {"directory": directories.get(session_id, "/workspace")},
                    }
                },
            )
        )


def _mock_prompt(respx_mock, waiting):
    """`waiting` session has an open form; everyone else answers nothing."""
    for session_id in {waiting, "ses_new", "ses_one"}:
        forms = (
            [{"id": "frm_1", "title": "Which repository?", "fields": []}]
            if session_id == waiting
            else []
        )
        respx_mock.get(f"http://opencode.test/api/session/{session_id}/form").mock(
            return_value=httpx.Response(200, json={"data": forms})
        )
        respx_mock.get(f"http://opencode.test/api/session/{session_id}/permission").mock(
            return_value=httpx.Response(200, json={"data": []})
        )


def _mock_turn_finishes(respx_mock, session_id):
    """A new session that answers on the first prompt and ends idle."""
    respx_mock.get(f"http://opencode.test/api/session/{session_id}/form").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get(f"http://opencode.test/api/session/{session_id}/permission").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.post(f"http://opencode.test/api/session/{session_id}/prompt").mock(
        return_value=httpx.Response(200, json={"data": {"id": "msg_1"}})
    )
    respx_mock.post(f"http://opencode.test/api/experimental/session/{session_id}/wait").mock(
        return_value=httpx.Response(200, json={"data": {"idle": True}})
    )
    respx_mock.get(f"http://opencode.test/api/session/{session_id}/diff").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get(f"http://opencode.test/api/session/{session_id}").mock(
        return_value=httpx.Response(200, json={"data": {"id": session_id, "outcome": "succeeded"}})
    )
    respx_mock.get(f"http://opencode.test/api/session/{session_id}/message").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "msg_1",
                        "type": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    },
                    {"id": "msg_2", "type": "idle", "outcome": "succeeded"},
                ],
                "cursor": {"previous": None, "next": None},
            },
        )
    )


class TestRunTask:
    def test_a_long_turn_returns_running_and_keeps_the_session(self, respx_mock):
        calls = {"n": 0}

        def active(request):
            calls["n"] += 1
            running = {} if calls["n"] == 1 else {"ses_new": {"type": "running"}}
            return httpx.Response(200, json={"data": running})

        respx_mock.get("http://opencode.test/api/session/active").mock(side_effect=active)
        respx_mock.post("http://opencode.test/api/session").mock(
            return_value=httpx.Response(200, json={"data": {"id": "ses_new"}})
        )
        respx_mock.post("http://opencode.test/api/session/ses_new/prompt").mock(
            return_value=httpx.Response(200, json={"data": {"id": "msg_1"}})
        )
        wait = respx_mock.post("http://opencode.test/api/experimental/session/ses_new/wait").mock(
            side_effect=httpx.ReadTimeout("budget spent")
        )
        respx_mock.get("http://opencode.test/api/session/ses_new/form").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx_mock.get("http://opencode.test/api/session/ses_new/permission").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx_mock.get("http://opencode.test/api/session/ses_new/message").mock(
            return_value=httpx.Response(
                200, json={"data": [], "cursor": {"previous": None, "next": None}}
            )
        )

        out = tools.run_task("do the thing", wait_seconds=5)

        assert "status: running" in out
        assert "session: ses_new" in out
        assert wait.called

    def test_a_second_task_is_refused_before_a_session_is_created(self, respx_mock):
        _mock_active(respx_mock, {"ses_busy": {"type": "running"}})
        create = respx_mock.post("http://opencode.test/api/session").mock(
            return_value=httpx.Response(200, json={"data": {"id": "ses_new"}})
        )

        out = tools.run_task("another task")

        assert "status: busy" in out
        assert "session: ses_busy" in out
        assert not create.called

    def test_a_parked_session_does_not_consume_the_only_slot(self, respx_mock):
        _mock_active(respx_mock, {"ses_waiting": {"type": "running"}})
        _mock_prompt(respx_mock, waiting="ses_waiting")
        create = respx_mock.post("http://opencode.test/api/session").mock(
            return_value=httpx.Response(200, json={"data": {"id": "ses_new"}})
        )
        _mock_turn_finishes(respx_mock, "ses_new")

        out = tools.run_task("do it while the owner thinks")

        assert "status: idle" in out
        assert "session: ses_new" in out
        assert create.called

    def test_capacity_is_the_configured_limit(self, respx_mock, monkeypatch):
        monkeypatch.setenv("OPENCODE_MAX_CONCURRENT", "2")
        _mock_active(respx_mock, {"ses_one": {"type": "running"}, "ses_two": {"type": "running"}})
        create = respx_mock.post("http://opencode.test/api/session").mock(
            return_value=httpx.Response(200, json={"data": {"id": "ses_new"}})
        )

        out = tools.run_task("third task")

        assert "status: busy" in out
        assert "2 of 2 slots in use" in out
        assert not create.called

    def test_a_second_slot_is_used_when_the_limit_allows_it(self, respx_mock, monkeypatch):
        monkeypatch.setenv("OPENCODE_MAX_CONCURRENT", "2")
        _mock_active(respx_mock, {"ses_one": {"type": "running"}})
        create = respx_mock.post("http://opencode.test/api/session").mock(
            return_value=httpx.Response(200, json={"data": {"id": "ses_new"}})
        )
        _mock_turn_finishes(respx_mock, "ses_new")

        out = tools.run_task("second task", directory="issue-117")

        assert "status: idle" in out
        assert create.called

    def test_two_turns_in_one_checkout_are_refused_whatever_the_limit(
        self, respx_mock, monkeypatch
    ):
        monkeypatch.setenv("OPENCODE_MAX_CONCURRENT", "4")
        _mock_active(respx_mock, {"ses_one": {"type": "running"}}, {"ses_one": "/workspace"})
        create = respx_mock.post("http://opencode.test/api/session").mock(
            return_value=httpx.Response(200, json={"data": {"id": "ses_new"}})
        )

        out = tools.run_task("same checkout")

        assert "status: busy" in out
        assert not create.called

    def test_a_second_checkout_runs_alongside(self, respx_mock, monkeypatch):
        monkeypatch.setenv("OPENCODE_MAX_CONCURRENT", "4")
        _mock_active(
            respx_mock, {"ses_one": {"type": "running"}}, {"ses_one": "/workspace/datahub-local"}
        )
        create = respx_mock.post("http://opencode.test/api/session").mock(
            return_value=httpx.Response(200, json={"data": {"id": "ses_new"}})
        )
        _mock_turn_finishes(respx_mock, "ses_new")

        out = tools.run_task("other repo", directory="datahub-local-core")

        assert "status: idle" in out
        assert create.called

    def test_continuing_a_running_session_is_refused(self, respx_mock):
        _mock_active(respx_mock, {"ses_one": {"type": "running"}})
        prompt = respx_mock.post("http://opencode.test/api/session/ses_one/prompt").mock(
            return_value=httpx.Response(200, json={"data": {"id": "msg_1"}})
        )

        out = tools.run_task("more work", session_id="ses_one")

        assert "status: busy" in out
        assert not prompt.called

    def test_a_directory_that_escapes_the_workspace_sends_nothing(self, respx_mock):
        out = tools.run_task("escape", directory="../../etc")

        assert out.startswith("ERROR:")
        assert "escapes the workspace" in out
        assert len(respx_mock.calls) == 0

    def test_an_absolute_directory_is_refused(self, respx_mock):
        out = tools.run_task("escape", directory="/etc")

        assert out.startswith("ERROR:")
        assert "workspace-relative" in out
        assert len(respx_mock.calls) == 0

    def test_the_wait_is_clamped_to_the_maximum(self, respx_mock):
        assert tools.settings.wait_seconds(10_000) == tools.settings.MAX_WAIT_SECONDS
        assert tools.settings.wait_seconds(None) == tools.settings.DEFAULT_WAIT_SECONDS


class TestGetSession:
    def test_idle_reports_the_answer_and_the_changed_files(self, respx_mock):
        _mock_idle_session(
            respx_mock,
            text="all tests pass",
            files=[
                {
                    "file": "src/a.py",
                    "patch": "",
                    "additions": 2,
                    "deletions": 1,
                    "status": "modified",
                }
            ],
        )

        out = tools.get_session("ses_x")

        assert "status: idle" in out
        assert "all tests pass" in out
        assert "M src/a.py (+2 -1)" in out

    def test_failure_is_reported_as_error_with_its_message(self, respx_mock):
        _mock_idle_session(respx_mock, text="", outcome="failed")
        respx_mock.get("http://opencode.test/api/session/ses_x/message").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "msg_2",
                            "type": "idle",
                            "outcome": "failed",
                            "error": {"type": "ProviderError", "message": "model exploded"},
                        }
                    ],
                    "cursor": {"previous": None, "next": None},
                },
            )
        )

        out = tools.get_session("ses_x")

        assert "status: error" in out
        assert "model exploded" in out

    def test_pending_input_wins_over_running(self, respx_mock):
        respx_mock.get("http://opencode.test/api/session/ses_x/form").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "frm_1",
                            "sessionID": "ses_x",
                            "title": "Which branch?",
                            "fields": [{"key": "branch", "type": "string", "required": True}],
                        }
                    ]
                },
            )
        )
        respx_mock.get("http://opencode.test/api/session/ses_x/permission").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx_mock.get("http://opencode.test/api/session/ses_x/message").mock(
            return_value=httpx.Response(
                200, json={"data": [], "cursor": {"previous": None, "next": None}}
            )
        )
        respx_mock.get("http://opencode.test/api/session/active").mock(
            return_value=httpx.Response(200, json={"data": {"ses_x": {"type": "running"}}})
        )

        out = tools.get_session("ses_x")

        assert "status: needs_input" in out
        assert "form: frm_1" in out
        assert "Which branch?" in out


class TestRespond:
    def test_a_single_field_form_is_mapped_by_its_key(self, respx_mock):
        _mock_idle_session(respx_mock)
        answered = {"n": 0}

        def forms(request):
            # The form is pending until the reply lands; the read after `respond`
            # must not keep reporting it forever.
            answered["n"] += 1
            data = (
                []
                if answered["n"] > 1
                else [
                    {
                        "id": "frm_1",
                        "sessionID": "ses_x",
                        "title": "Continue?",
                        "fields": [{"key": "answer", "type": "boolean"}],
                    }
                ]
            )
            return httpx.Response(200, json={"data": data})

        respx_mock.get("http://opencode.test/api/session/ses_x/form").mock(side_effect=forms)
        reply = respx_mock.post("http://opencode.test/api/session/ses_x/form/frm_1/reply").mock(
            return_value=httpx.Response(200, json={"data": {"id": "frm_1"}})
        )

        out = tools.respond("ses_x", "yes", form_id="frm_1")

        assert reply.called
        assert json.loads(reply.calls[0].request.content) == {"answer": {"answer": True}}
        assert "status: idle" in out

    def test_a_permission_decision_is_posted_verbatim(self, respx_mock):
        reply = respx_mock.post(
            "http://opencode.test/api/session/ses_x/permission/per_1/reply"
        ).mock(return_value=httpx.Response(200, json={"data": {"id": "per_1", "effect": "allow"}}))
        _mock_idle_session(respx_mock)

        out = tools.respond("ses_x", "once", request_id="per_1")

        assert reply.called
        assert json.loads(reply.calls[0].request.content) == {"decision": "once"}
        assert "status: idle" in out

    def test_an_unknown_decision_is_refused_without_a_request(self, respx_mock):
        out = tools.respond("ses_x", "maybe", request_id="per_1")

        assert out.startswith("ERROR:")
        assert len(respx_mock.calls) == 0


class TestAbort:
    def test_interrupt_reports_whether_a_turn_was_stopped(self, respx_mock):
        respx_mock.post("http://opencode.test/api/session/ses_x/interrupt").mock(
            return_value=httpx.Response(200, json={"interrupted": True})
        )
        _mock_idle_session(respx_mock)

        out = tools.abort_session("ses_x")

        assert out.startswith("interrupted: true")
        assert "status: idle" in out


class TestOutputBudget:
    def test_a_long_answer_is_truncated_with_the_marker(self, respx_mock):
        _mock_idle_session(respx_mock, text="x" * 5000)
        registry = Registry(name="mcp-opencode")
        registry.add(
            "get_session",
            "test",
            tools.get_session,
            schema={"type": "object", "properties": {}},
            budget=200,
        )

        out = registry.call("get_session", {"session_id": "ses_x"})

        assert "TRUNCATED" in out
        assert len(out.encode("utf-8")) <= 200


class TestGetDiff:
    def test_the_patch_is_returned_with_its_file_header(self, respx_mock):
        captured = {}

        def diff(request):
            captured.update(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "file": "src/a.py",
                            "patch": "@@ -1 +1 @@\n-old\n+new",
                            "additions": 1,
                            "deletions": 1,
                            "status": "modified",
                        }
                    ]
                },
            )

        respx_mock.get("http://opencode.test/api/session/ses_x/diff").mock(side_effect=diff)

        out = tools.get_diff("ses_x", from_message="msg_1", context=3)

        assert "M src/a.py (+1 -1)" in out
        assert "+new" in out
        assert captured == {"from": "msg_1", "context": "3"}

    def test_an_unchanged_session_says_so(self, respx_mock):
        respx_mock.get("http://opencode.test/api/session/ses_x/diff").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        out = tools.get_diff("ses_x")

        assert "changes: none" in out


class TestListSessions:
    def test_recent_sessions_are_filtered_and_rendered(self, respx_mock):
        captured = {}

        def sessions(request):
            captured.update(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "ses_one",
                            "title": "fix the parser",
                            "outcome": "succeeded",
                            "time": {"created": 1793481328000, "updated": 1793481328000},
                        }
                    ],
                    "cursor": {"previous": None, "next": None},
                },
            )

        respx_mock.get("http://opencode.test/api/session").mock(side_effect=sessions)

        out = tools.list_sessions(limit=5, search="parser")

        assert "ses_one" in out
        assert "fix the parser" in out
        assert "succeeded" in out
        assert captured["search"] == "parser"
        assert captured["limit"] == "5"
        assert captured["order"] == "desc"


class TestGetTranscript:
    def test_messages_are_chronological_and_tools_are_named(self, respx_mock):
        captured = {}

        def messages(request):
            captured.update(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    # order=desc: the API returns newest first, the tool reverses it.
                    "data": [
                        {
                            "id": "msg_2",
                            "type": "assistant",
                            "content": [
                                {"type": "text", "text": "done"},
                                {"type": "tool", "name": "shell", "state": {"status": "error"}},
                            ],
                        },
                        {"id": "msg_1", "type": "user", "text": "run the tests"},
                    ],
                    "cursor": {"previous": None, "next": None},
                },
            )

        respx_mock.get("http://opencode.test/api/session/ses_x/message").mock(side_effect=messages)

        out = tools.get_transcript("ses_x", limit=5)

        assert out.index("user: run the tests") < out.index("assistant: done")
        assert "tool: shell (error)" in out
        assert captured["limit"] == "5"
        assert captured["order"] == "desc"

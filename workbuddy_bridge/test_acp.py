from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from workbuddy_bridge.acp import (
    AcpClient,
    DesktopServer,
    GatewayCancelledError,
    GatewayTimeoutError,
    WorkBuddyError,
    _iter_sse_json,
    _session_events,
    _session_title,
    _summarize_gateway_observability,
    gateway_post_run,
    gateway_stream_run,
    spawn_isolated_server,
)


class SessionTitleTests(unittest.TestCase):
    def test_uses_workbuddy_generated_title(self) -> None:
        events = [
            {
                "params": {
                    "update": {
                        "sessionUpdate": "session_info_update",
                        "title": "  WorkBuddy generated title  ",
                    }
                }
            }
        ]

        self.assertEqual(_session_title(events), "WorkBuddy generated title")

    def test_ignores_non_title_updates(self) -> None:
        events = [
            {
                "params": {
                    "update": {
                        "sessionUpdate": "session_info_update",
                        "_meta": {"codebuddy.ai/agentPhase": {"phase": "idle"}},
                    }
                }
            }
        ]

        self.assertEqual(_session_title(events), "")

    def test_filters_broadcasts_by_session_id(self) -> None:
        events = [
            {"params": {"sessionId": "one", "update": {"text": "first"}}},
            {"params": {"sessionId": "two", "update": {"text": "second"}}},
        ]

        self.assertEqual(_session_events(events, "two"), [events[1]])


class TaskSessionTests(unittest.TestCase):
    def test_new_session_uses_workbuddy_task_metadata(self) -> None:
        server = DesktopServer("http://localhost/api/v1/acp", "pw", "host", "pipe", 1)
        client = AcpClient(server)
        captured: dict[str, Any] = {}

        def request(method: str, params: dict[str, Any], **_: Any) -> dict[str, str]:
            captured["method"] = method
            captured["params"] = params
            return {"sessionId": "session-1"}

        client.request = request  # type: ignore[method-assign]
        try:
            self.assertEqual(client.new_session("."), "session-1")
        finally:
            client.close()

        self.assertEqual(captured["method"], "session/new")
        self.assertEqual(captured["params"]["cwd"], "")
        self.assertEqual(
            captured["params"]["_meta"]["codebuddy.ai"],
            {"welcomeMode": "working", "isPlayground": True},
        )

    def test_load_session_reuses_the_requested_workbuddy_task(self) -> None:
        server = DesktopServer("http://localhost/api/v1/acp", "pw", "host", "pipe", 1)
        client = AcpClient(server)
        captured: dict[str, Any] = {}

        def request(method: str, params: dict[str, Any], **_: Any) -> dict[str, str]:
            captured["method"] = method
            captured["params"] = params
            return {}

        client.request = request  # type: ignore[method-assign]
        try:
            self.assertEqual(client.load_session("session-1", "."), "session-1")
        finally:
            client.close()

        self.assertEqual(captured["method"], "session/load")
        self.assertEqual(captured["params"]["sessionId"], "session-1")
        self.assertEqual(captured["params"]["cwd"], "")
        self.assertEqual(
            captured["params"]["_meta"]["codebuddy.ai"],
            {"welcomeMode": "working", "isPlayground": True},
        )

    def test_permission_requests_are_always_allowed(self) -> None:
        server = DesktopServer("http://localhost/api/v1/acp", "pw", "host", "pipe", 1)
        client = AcpClient(server)
        event = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {
                "options": [
                    {"kind": "allow_always", "optionId": "allow_always"},
                    {"kind": "allow_once", "optionId": "allow"},
                    {"kind": "reject_once", "optionId": "reject"},
                ],
                "toolCall": {
                    "_meta": {"codebuddy.ai/toolName": "WebFetch"}
                },
            },
        }
        response = Mock()
        response.raise_for_status = Mock()
        with patch("workbuddy_bridge.acp.httpx.Client") as http_client:
            http_client.return_value.__enter__.return_value.post.return_value = response
            client._grant_permission(event)
            payload = http_client.return_value.__enter__.return_value.post.call_args.kwargs[
                "json"
            ]
        client.close()

        self.assertEqual(
            payload["result"],
            {"outcome": {"outcome": "selected", "optionId": "allow_always"}},
        )

    def test_prompt_can_target_an_explicit_session(self) -> None:
        client = object.__new__(AcpClient)
        client.session_id = "most-recent-session"
        client.is_playground = True
        captured: dict[str, Any] = {}

        def request(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, str]:
            captured["method"] = method
            captured["params"] = params
            captured["kwargs"] = kwargs
            callback = kwargs["event_callback"]
            callback(
                {
                    "params": {
                        "sessionId": "target-session",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "shared"},
                        },
                    }
                }
            )
            return {"stopReason": "end_turn"}

        client.request = request  # type: ignore[method-assign]
        response = client.prompt("hello", session_id="target-session")

        self.assertEqual(captured["method"], "session/prompt")
        self.assertEqual(captured["params"]["sessionId"], "target-session")
        self.assertEqual(response["session_id"], "target-session")
        self.assertEqual(response["answer"], "shared")

    def test_isolated_runtime_uses_workbuddy_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "WorkBuddy.exe"
            cli_path = root / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"
            executable.touch()
            cli_path.parent.mkdir(parents=True)
            cli_path.touch()
            config_dir = root / "profile"
            desktop = DesktopServer(
                "http://localhost:1/api/v1/acp",
                "pw",
                "host",
                "pipe",
                1,
                2,
            )
            ready = Mock()
            ready.is_success = True
            http_client = Mock()
            http_client.__enter__ = Mock(return_value=http_client)
            http_client.__exit__ = Mock(return_value=False)
            http_client.get.return_value = ready

            with (
                patch.dict(os.environ, {"WORKBUDDY_CONFIG_DIR": str(config_dir)}),
                patch("workbuddy_bridge.acp._process_executable", return_value=executable),
                patch("workbuddy_bridge.acp._free_local_port", return_value=54321),
                patch(
                    "workbuddy_bridge.acp._rpc",
                    return_value={
                        "acpEndpoint": "http://127.0.0.1:54321/api/v1/acp",
                        "pid": 9,
                    },
                ) as rpc,
                patch("workbuddy_bridge.acp.httpx.Client", return_value=http_client),
            ):
                runtime = spawn_isolated_server(
                    desktop,
                    temp_dir,
                    session_id="existing-session",
                )

            params = rpc.call_args.args[2]
            self.assertEqual(
                params["env"]["CODEBUDDY_CONFIG_DIR"],
                str(config_dir.resolve()),
            )
            self.assertEqual(
                params["args"][-2:],
                ["--session-id", "existing-session"],
            )
            self.assertTrue(runtime.session_host_id.startswith("codex-worker-"))


class GatewayRunsTests(unittest.TestCase):
    """Gateway Runs API single-request path (POST /api/v1/runs)."""

    def _server(self) -> DesktopServer:
        return DesktopServer(
            acp_endpoint="http://127.0.0.1:9/api/v1/acp",
            password="",
            session_host_id="codex-worker-abc",
            sidecar_pipe=r"\\.\pipe\test",
            sidecar_pid=1,
            runtime_pid=2,
        )

    def test_sse_parser_accepts_compact_data_and_reports_drops(self) -> None:
        response = Mock()
        response.iter_lines.return_value = [
            ": keepalive",
            "data:{\"status\":\"streaming\"}",
            "",
            "data: {\"status\":",
            "data: \"completed\"}",
            "",
            "data: not-json",
            "",
        ]
        diagnostics: dict[str, int] = {}
        events = list(_iter_sse_json(response, diagnostics))
        self.assertEqual(events, [
            {"status": "streaming"},
            {"status": "completed"},
        ])
        self.assertEqual(diagnostics["parsed_event_count"], 2)
        self.assertEqual(diagnostics["dropped_event_count"], 1)
        self.assertEqual(diagnostics["ignored_event_count"], 1)

    def test_post_run_sends_generic_message_and_returns_run_id(self) -> None:
        server = self._server()
        captured: dict[str, Any] = {}
        response = Mock()
        response.raise_for_status = Mock()
        response.json = Mock(return_value={"data": {"runId": "run-1", "status": "accepted"}})
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)

        def post(url: str, headers: dict[str, Any], json: dict[str, Any]) -> Any:
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return response

        client.post = post
        with patch("workbuddy_bridge.acp.httpx.Client", return_value=client):
            run_id = gateway_post_run(server, "do it", conversation_id="conv-1")

        self.assertEqual(run_id, "run-1")
        self.assertTrue(captured["url"].endswith("/api/v1/runs"))
        body = captured["json"]
        self.assertEqual(body["type"], "message")
        self.assertEqual(body["text"], "do it")
        self.assertTrue(body["id"])
        self.assertEqual(body["source"]["platform"], "generic")
        self.assertEqual(body["source"]["conversation"]["id"], "conv-1")
        # localhost host has no password => no Authorization header
        self.assertNotIn("Authorization", captured["headers"])

    def test_stream_run_collects_chunks_session_and_final_markdown(self) -> None:
        server = self._server()
        stream_events = [
            {"status": "accepted", "replyTo": "m1"},
            {"status": "streaming", "content": {"chunk": "He"}},
            {"status": "streaming", "content": {"chunk": "llo"}},
            {
                "status": "completed",
                "content": {"markdown": "Hello world"},
                "agent": {"sessionId": "sess-9", "toolCalls": [{"name": "Read"}]},
            },
        ]
        response = Mock()
        response.status_code = 200
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)
        seen: list[dict[str, Any]] = []
        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=client),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=iter(stream_events)),
        ):
            result = gateway_stream_run(
                server, "run-1", timeout_seconds=5.0, event_callback=seen.append
            )

        self.assertEqual(result["session_id"], "sess-9")
        self.assertEqual(result["answer"], "Hello world")
        self.assertEqual(result["result"]["stopReason"], "end_turn")
        self.assertEqual(result["result"]["toolCalls"], [{"name": "Read"}])
        self.assertEqual(len(seen), 4)

    def test_stream_run_recovers_same_session_history_after_empty_completed_event(self) -> None:
        """A terminal SSE event without text may still have a safe session reply."""
        server = self._server()
        stream_events = [
            {"status": "completed", "agent": {"sessionId": "sess-9"}},
        ]
        response = Mock()
        response.status_code = 200
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)
        history = {"name": "Recovered", "requests": [{"finalReply": "Recovered answer"}]}
        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=client),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=iter(stream_events)),
            patch("workbuddy_bridge.acp._gateway_history", return_value=history) as get_history,
        ):
            result = gateway_stream_run(server, "run-1", timeout_seconds=5.0)

        self.assertEqual(result["answer"], "Recovered answer")
        get_history.assert_called_with(server, "sess-9")

    def test_stream_run_raises_on_error_event(self) -> None:
        server = self._server()
        stream_events = [
            {"status": "error", "error": {"code": "EXECUTION_ERROR", "message": "boom"}},
        ]
        response = Mock()
        response.status_code = 200
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)
        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=client),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=iter(stream_events)),
        ):
            with self.assertRaises(Exception) as ctx:
                gateway_stream_run(server, "run-1", timeout_seconds=5.0)
        self.assertIn("boom", str(ctx.exception))

    def test_stream_run_raises_on_cancelled_terminal(self) -> None:
        """A cancelled terminal event raises GatewayCancelledError."""
        server = self._server()
        stream_events = [
            {"status": "cancelled", "agent": {"sessionId": "sess-9"}},
        ]
        response = Mock()
        response.status_code = 200
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)
        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=client),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=iter(stream_events)),
        ):
            with self.assertRaises(GatewayCancelledError):
                gateway_stream_run(server, "run-1", timeout_seconds=5.0)

    def test_stream_activity_extends_idle_deadline(self) -> None:
        server = self._server()
        response = Mock(status_code=200)
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)

        def events():
            yield {"status": "streaming", "content": {"chunk": "a"}}
            time.sleep(0.06)
            yield {"status": "streaming", "content": {"chunk": "b"}}
            time.sleep(0.06)
            yield {"status": "completed", "content": {"markdown": "ab"}}

        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=client),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=events()),
        ):
            result = gateway_stream_run(
                server,
                "run-1",
                max_task_duration_seconds=1.0,
                idle_timeout_seconds=0.1,
            )
        self.assertEqual(result["answer"], "ab")

    def test_idle_timeout_is_distinct_from_max_duration(self) -> None:
        server = self._server()
        response = Mock(status_code=200)
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)

        def events():
            yield {"status": "streaming", "content": {"chunk": "a"}}
            time.sleep(0.3)
            yield {"status": "completed", "content": {"markdown": "late"}}

        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=client),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=events()),
        ):
            with self.assertRaises(WorkBuddyError) as ctx:
                gateway_stream_run(
                    server,
                    "run-1",
                    max_task_duration_seconds=1.0,
                    idle_timeout_seconds=0.05,
                )
        self.assertIn("idle_timeout", str(ctx.exception))
        self.assertIsInstance(ctx.exception, GatewayTimeoutError)
        self.assertEqual(ctx.exception.timeout_reason, "idle_timeout")

    def test_stream_run_rejects_non_terminal_empty_stream(self) -> None:
        """A zero-event/non-terminal stream must not be reported as completed."""
        server = self._server()
        response = Mock()
        response.status_code = 503
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)
        with patch("workbuddy_bridge.acp.httpx.Client", return_value=client):
            with self.assertRaises(WorkBuddyError) as ctx:
                gateway_stream_run(server, "run-1", timeout_seconds=5.0)
        self.assertIn("did not yield a final reply", str(ctx.exception))

    def test_concurrent_runs_without_sse_session_id_do_not_share_global_latest_session(self) -> None:
        """Missing SSE metadata must remain unknown, not cross-correlate runs.

        The desktop ``/sessions`` endpoint is global.  When two Gateway runs
        finish together and neither SSE stream includes ``agent.sessionId``,
        reading its newest entry would make both Bridge tasks claim the same
        session.  This regression test deliberately overlaps two such streams.
        """
        server = self._server()
        barrier = threading.Barrier(2)

        class Response:
            status_code = 200

            def __init__(self, text: str) -> None:
                self.events = [{"status": "completed", "content": {"markdown": text}}]

        class Stream:
            def __init__(self, response: Response) -> None:
                self.response = response

            def __enter__(self) -> Response:
                barrier.wait(timeout=2.0)
                return self.response

            def __exit__(self, *args: Any) -> bool:
                return False

        class Client:
            def __init__(self, response: Response) -> None:
                self.response = response

            def __enter__(self) -> "Client":
                return self

            def __exit__(self, *args: Any) -> bool:
                return False

            def stream(self, *args: Any, **kwargs: Any) -> Stream:
                return Stream(self.response)

        responses = iter([Response("one"), Response("two")])
        results: dict[str, dict[str, Any]] = {}
        errors: list[BaseException] = []

        def run(run_id: str) -> None:
            try:
                results[run_id] = gateway_stream_run(server, run_id, timeout_seconds=5.0)
            except BaseException as exc:  # assertion failures must fail the test
                errors.append(exc)

        with (
            patch("workbuddy_bridge.acp.httpx.Client", side_effect=lambda *a, **kw: Client(next(responses))),
            patch("workbuddy_bridge.acp._iter_sse_json", side_effect=lambda response, diagnostics=None: iter(response.events)),
        ):
            first = threading.Thread(target=run, args=("run-a",))
            second = threading.Thread(target=run, args=("run-b",))
            first.start()
            second.start()
            first.join(timeout=3.0)
            second.join(timeout=3.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results["run-a"]["session_id"], "")
        self.assertEqual(results["run-b"]["session_id"], "")


class GatewayObservabilityTests(unittest.TestCase):
    """Content-free observability digest for the Gateway SSE stream."""

    def _server(self) -> DesktopServer:
        return DesktopServer(
            acp_endpoint="http://127.0.0.1:9/api/v1/acp",
            password="",
            session_host_id="codex-worker-abc",
            sidecar_pipe=r"\\.\pipe\test",
            sidecar_pid=1,
            runtime_pid=2,
        )

    def _run_with_events(self, stream_events: list[dict[str, Any]]) -> dict[str, Any]:
        server = self._server()
        response = Mock()
        response.status_code = 200
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)
        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=client),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=iter(stream_events)),
        ):
            return gateway_stream_run(server, "run-1", timeout_seconds=5.0)

    def test_observability_present_in_completed_gateway_result(self) -> None:
        """Gateway completed task result retains the observability digest."""
        result = self._run_with_events([
            {"status": "accepted", "replyTo": "m1"},
            {"status": "streaming", "content": {"chunk": "Hi"}},
            {
                "status": "completed",
                "content": {"markdown": "Hi there"},
                "agent": {"sessionId": "sess-1", "toolCalls": []},
            },
        ])
        self.assertIn("observability", result)
        obs = result["observability"]
        self.assertEqual(obs["event_count"], 3)
        self.assertEqual(obs["status_counts"].get("streaming"), 1)
        self.assertEqual(obs["status_counts"].get("completed"), 1)
        self.assertTrue(obs["has_stream_chunk"])
        self.assertTrue(obs["has_final_content"])

    def test_observability_detects_usage_and_tokens(self) -> None:
        """Fake SSE with usage/token fields is detected with whitelisted values."""
        result = self._run_with_events([
            {"status": "accepted"},
            {
                "status": "completed",
                "content": {"markdown": "ok"},
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "total_tokens": 20,
                    "model_name": "hy3",
                },
                "agent": {"sessionId": "sess-1"},
            },
        ])
        obs = result["observability"]
        self.assertTrue(obs["has_usage"])
        self.assertTrue(obs["has_token"])
        # Whitelisted numeric token values are surfaced
        self.assertEqual(obs["token_values"].get("usage.input_tokens"), 12)
        self.assertEqual(obs["token_values"].get("usage.output_tokens"), 8)
        self.assertEqual(obs["token_values"].get("usage.total_tokens"), 20)
        # Unknown usage/token scalar (model_name) recorded as path+type, not value
        paths = {u["path"] for u in obs["unknown_field_paths"]}
        self.assertIn("usage.model_name", paths)
        # The unknown-field entries must NOT carry string content values
        for entry in obs["unknown_field_paths"]:
            self.assertNotIn("value", entry)
        self.assertFalse(obs["usage_without_recognized_tokens"])

    def test_observability_detects_thought_reasoning_events_without_content(self) -> None:
        """Fake SSE with thought/reasoning markers counted, but text not leaked."""
        result = self._run_with_events([
            {"status": "accepted"},
            {"status": "streaming", "thought": {"text": "SECRET THOUGHT BODY"}},
            {"status": "streaming", "reasoning": {"content": "SECRET REASONING"}},
            {
                "status": "completed",
                "content": {"markdown": "answer"},
                "agent": {"sessionId": "sess-1"},
            },
        ])
        obs = result["observability"]
        self.assertTrue(obs["has_thought"])
        self.assertTrue(obs["has_reasoning"])
        self.assertEqual(obs["thought_event_count"], 2)
        # The observability digest must not contain thought/reasoning text
        blob = json.dumps(obs, ensure_ascii=False)
        self.assertNotIn("SECRET THOUGHT BODY", blob)
        self.assertNotIn("SECRET REASONING", blob)

    def test_observability_detects_thought_event_type_without_content(self) -> None:
        """Thought markers in an SSE event type are detected and redacted."""
        result = self._run_with_events([
            {"status": "streaming", "type": "agent_thought_chunk", "content": {"chunk": "SECRET"}},
            {
                "status": "completed",
                "content": {"markdown": "answer"},
                "agent": {"sessionId": "sess-1"},
            },
        ])
        obs = result["observability"]
        self.assertTrue(obs["has_thought"])
        self.assertEqual(obs["thought_event_count"], 1)
        self.assertEqual(obs["type_counts"].get("agent_thought_chunk"), 1)
        self.assertNotIn("SECRET", json.dumps(obs, ensure_ascii=False))

    def test_observability_no_usage_no_thought(self) -> None:
        """Fake SSE without usage/thought fields reports all-absent flags."""
        result = self._run_with_events([
            {"status": "accepted"},
            {"status": "streaming", "content": {"chunk": "x"}},
            {
                "status": "completed",
                "content": {"markdown": "x"},
                "agent": {"sessionId": "sess-1"},
            },
        ])
        obs = result["observability"]
        self.assertFalse(obs["has_usage"])
        self.assertFalse(obs["has_token"])
        self.assertFalse(obs["has_thought"])
        self.assertFalse(obs["has_reasoning"])
        self.assertEqual(obs["thought_event_count"], 0)
        self.assertTrue(obs["has_stream_chunk"])
        self.assertTrue(obs["has_final_content"])
        self.assertEqual(obs["token_values"], {})
        self.assertFalse(obs["usage_without_recognized_tokens"])

    def test_observability_usage_without_recognized_tokens(self) -> None:
        """usage present but no whitelisted token numbers sets the flag."""
        result = self._run_with_events([
            {"status": "accepted"},
            {
                "status": "completed",
                "content": {"markdown": "ok"},
                "usage": {"cost": 0.01, "currency": "CNY"},
                "agent": {"sessionId": "sess-1"},
            },
        ])
        obs = result["observability"]
        self.assertTrue(obs["has_usage"])
        self.assertTrue(obs["usage_without_recognized_tokens"])
        self.assertEqual(obs["token_values"], {})
        # Unknown usage scalars recorded as path+type only
        paths = {u["path"] for u in obs["unknown_field_paths"]}
        self.assertIn("usage.cost", paths)
        self.assertIn("usage.currency", paths)

    def test_observability_does_not_leak_prompt_or_answer(self) -> None:
        """The digest must never contain prompt or answer text."""
        result = self._run_with_events([
            {"status": "accepted", "prompt": "request payload text"},
            {
                "status": "completed",
                "content": {"markdown": "request payload text"},
                "agent": {"sessionId": "sess-1"},
            },
        ])
        obs = result["observability"]
        blob = json.dumps(obs, ensure_ascii=False)
        self.assertNotIn("request payload text", blob)

    def test_raw_events_still_returned_but_observability_is_separate(self) -> None:
        """events list is still returned (unchanged); observability is separate."""
        events = [
            {"status": "accepted"},
            {"status": "completed", "content": {"markdown": "ok"},
             "agent": {"sessionId": "sess-1"}},
        ]
        result = self._run_with_events(events)
        # events list preserved (callers may still need it internally)
        self.assertEqual(len(result["events"]), 2)
        # observability is a separate digest, not the raw events
        self.assertIsNot(result["observability"], result["events"])
        self.assertEqual(result["observability"]["event_count"], 2)


if __name__ == "__main__":
    unittest.main()

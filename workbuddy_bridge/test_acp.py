from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from workbuddy_bridge.acp import (
    AcpClient,
    DesktopServer,
    _session_events,
    _session_title,
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


if __name__ == "__main__":
    unittest.main()

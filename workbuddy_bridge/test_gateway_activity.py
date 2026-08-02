from __future__ import annotations

import time
import unittest
from unittest.mock import Mock, patch

from workbuddy_bridge.acp import DesktopServer, GatewayTimeoutError, gateway_stream_run


class GatewayActivityTests(unittest.TestCase):
    def _server(self) -> DesktopServer:
        return DesktopServer(
            acp_endpoint="http://127.0.0.1:9/api/v1/acp",
            password="",
            session_host_id="codex-worker-test",
            sidecar_pipe=r"\\.\pipe\test",
            sidecar_pid=1,
            runtime_pid=2,
        )

    def _client(self) -> Mock:
        response = Mock(status_code=200)
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)
        return client

    def test_thought_event_extends_idle_deadline(self) -> None:
        def events():
            yield {"status": "accepted"}
            time.sleep(0.10)
            yield {"type": "agent_thought_chunk"}
            time.sleep(0.10)
            yield {"status": "streaming", "content": {"chunk": "ok"}}
            time.sleep(0.10)
            yield {"status": "completed", "content": {"markdown": "ok"}}

        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=self._client()),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=events()),
        ):
            result = gateway_stream_run(
                self._server(),
                "run-thinking",
                max_task_duration_seconds=1.0,
                idle_timeout_seconds=0.15,
            )

        self.assertEqual(result["answer"], "ok")

    def test_keepalive_does_not_extend_idle_deadline(self) -> None:
        def events():
            yield {"status": "accepted"}
            time.sleep(0.10)
            yield {"type": "keepalive"}
            time.sleep(0.10)
            yield {"status": "completed", "content": {"markdown": "late"}}

        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=self._client()),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=events()),
        ):
            with self.assertRaises(GatewayTimeoutError) as context:
                gateway_stream_run(
                    self._server(),
                    "run-keepalive",
                    max_task_duration_seconds=1.0,
                    idle_timeout_seconds=0.15,
                )

        self.assertEqual(context.exception.timeout_reason, "idle_timeout")

    def test_streaming_event_alone_extends_idle_deadline(self) -> None:
        """A streaming status event is activity even without a chunk payload."""
        def events():
            yield {"status": "accepted"}
            time.sleep(0.10)
            yield {"status": "streaming"}
            time.sleep(0.10)
            yield {"status": "completed", "content": {"markdown": "ok"}}

        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=self._client()),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=events()),
        ):
            result = gateway_stream_run(
                self._server(),
                "run-streaming-only",
                max_task_duration_seconds=1.0,
                idle_timeout_seconds=0.15,
            )

        self.assertEqual(result["answer"], "ok")


if __name__ == "__main__":
    unittest.main()

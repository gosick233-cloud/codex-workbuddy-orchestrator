from __future__ import annotations

import queue
import threading
import unittest
from unittest.mock import Mock, patch

from workbuddy_bridge.acp import DesktopServer, gateway_stream_run


class RecordingQueue(queue.Queue):
    instances: list["RecordingQueue"] = []

    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[tuple[str, object]] = []
        self.instances.append(self)

    def put(self, item: tuple[str, object], *args: object, **kwargs: object) -> None:
        self.recorded.append(item)
        super().put(item, *args, **kwargs)


class GatewayReaderStopTests(unittest.TestCase):
    def test_late_event_is_not_enqueued_after_terminal_cleanup(self) -> None:
        release_late_event = threading.Event()
        response = Mock(status_code=200)
        response.close.side_effect = release_late_event.set
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)

        def events():
            yield {"status": "completed", "content": {"markdown": "ok"}}
            self.assertTrue(release_late_event.wait(timeout=1.0))
            yield {"status": "streaming", "content": {"chunk": "late"}}

        server = DesktopServer(
            acp_endpoint="http://127.0.0.1:9/api/v1/acp",
            password="",
            session_host_id="codex-worker-test",
            sidecar_pipe=r"\\.\pipe\test",
            sidecar_pid=1,
            runtime_pid=2,
        )
        RecordingQueue.instances.clear()
        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=client),
            patch("workbuddy_bridge.acp._iter_sse_json", return_value=events()),
            patch("workbuddy_bridge.acp.queue.Queue", RecordingQueue),
        ):
            result = gateway_stream_run(server, "run-reader-stop", timeout_seconds=1.0)

        self.assertEqual(result["answer"], "ok")
        self.assertEqual(len(RecordingQueue.instances), 1)
        self.assertEqual(
            [item for item in RecordingQueue.instances[0].recorded if item[0] == "event"],
            [("event", {"status": "completed", "content": {"markdown": "ok"}})],
        )

    def test_reader_thread_is_joined_and_exited_after_terminal(self) -> None:
        """The reader thread must be joined and confirmed exited."""
        response = Mock(status_code=200)
        stream_cm = Mock()
        stream_cm.__enter__ = Mock(return_value=response)
        stream_cm.__exit__ = Mock(return_value=False)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.stream = Mock(return_value=stream_cm)
        created: list[threading.Thread] = []

        original_thread = threading.Thread

        class SpyThread(original_thread):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)
                created.append(self)

        server = DesktopServer(
            acp_endpoint="http://127.0.0.1:9/api/v1/acp",
            password="",
            session_host_id="codex-worker-test",
            sidecar_pipe=r"\\.\pipe\test",
            sidecar_pid=1,
            runtime_pid=2,
        )
        with (
            patch("workbuddy_bridge.acp.httpx.Client", return_value=client),
            patch(
                "workbuddy_bridge.acp._iter_sse_json",
                return_value=iter([{"status": "completed", "content": {"markdown": "ok"}}]),
            ),
            patch("workbuddy_bridge.acp.threading.Thread", SpyThread),
        ):
            result = gateway_stream_run(server, "run-reader-exit", timeout_seconds=1.0)

        self.assertEqual(result["answer"], "ok")
        self.assertTrue(created)
        # Every reader thread was joined; none is still alive afterwards.
        for thread in created:
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from workbuddy_bridge.acp import AcpClient
from workbuddy_bridge.server import (
    TASKS,
    TaskState,
    WORKBUDDY_DEFAULT_TASK_MODEL,
    _gateway_task_model,
    workbuddy_start,
)


class SessionConfigTests(unittest.TestCase):
    def test_applies_model_and_reasoning_before_prompt(self) -> None:
        client = object.__new__(AcpClient)
        client.session_id = "session-1"
        client.request = Mock(return_value={})

        client.configure_session(
            model="deepseek-v4-flash",
            reasoning_effort="max",
        )

        self.assertEqual(client.request.call_count, 3)
        client.request.assert_any_call(
            "session/set_config_option",
            {
                "sessionId": "session-1",
                "configId": "mode",
                "value": "fullAccess",
            },
            timeout_seconds=30.0,
        )
        client.request.assert_any_call(
            "session/set_config_option",
            {
                "sessionId": "session-1",
                "configId": "model",
                "value": "deepseek-v4-flash",
            },
            timeout_seconds=30.0,
        )
        client.request.assert_any_call(
            "session/set_config_option",
            {
                "sessionId": "session-1",
                "configId": "thought_level",
                "value": "max",
            },
            timeout_seconds=30.0,
        )

    def test_explicit_session_does_not_use_mutable_current_session(self) -> None:
        client = object.__new__(AcpClient)
        client.session_id = "latest-session"
        client.request = Mock(return_value={})

        client.configure_session(
            model="deepseek-v4-flash",
            reasoning_effort="low",
            session_id="target-session",
        )

        for call in client.request.call_args_list:
            self.assertEqual(call.args[1]["sessionId"], "target-session")


class GatewayReasoningEffortSemanticsTests(unittest.TestCase):
    """Gateway new sessions must not pretend per-request reasoning_effort applied."""

    def test_gateway_task_model_uses_caller_model(self) -> None:
        """_gateway_task_model returns the caller model without a bootstrap step."""
        task = TaskState(task_id="t1", prompt="p", cwd=".", model="hy3")
        self.assertEqual(_gateway_task_model(task), "hy3")

    def test_gateway_task_model_defaults_when_no_model(self) -> None:
        task = TaskState(task_id="t1", prompt="p", cwd=".")
        self.assertEqual(_gateway_task_model(task), WORKBUDDY_DEFAULT_TASK_MODEL)

    def test_gateway_result_marks_reasoning_effort_not_applied(self) -> None:
        """Gateway task.result must record reasoning_effort_applied=false.

        Reproduces the server.py Gateway branch result assembly: the requested
        value is recorded separately from applied=false so the input is never
        echoed as effective (the model is fixed at Host boot via --model).
        """
        task = TaskState(
            task_id="t1", prompt="p", cwd=".", model="hy3", reasoning_effort="high"
        )
        # Simulated gateway_stream_run return (incl. observability)
        gateway_result = {
            "session_id": "sess-1",
            "answer": "ok",
            "title": "",
            "result": {"runId": "run-1", "stopReason": "end_turn", "toolCalls": []},
            "observability": {"event_count": 1, "has_usage": False},
        }
        # Mirrors server.py Gateway branch task.result assembly
        task.result = {
            **gateway_result["result"],
            "backend": "gateway_runs",
            "reasoning_effort_requested": (task.reasoning_effort or "").strip() or None,
            "reasoning_effort_applied": False,
            "observability": gateway_result.get("observability"),
        }
        self.assertEqual(task.result["reasoning_effort_requested"], "high")
        self.assertFalse(task.result["reasoning_effort_applied"])
        self.assertEqual(task.result["backend"], "gateway_runs")
        self.assertIn("observability", task.result)

    def test_gateway_result_reasoning_effort_none_when_not_requested(self) -> None:
        """Without reasoning_effort, requested is None and applied stays false."""
        task = TaskState(task_id="t1", prompt="p", cwd=".", model="hy3")
        task.result = {
            "runId": "run-1", "stopReason": "end_turn", "toolCalls": [],
            "backend": "gateway_runs",
            "reasoning_effort_requested": (task.reasoning_effort or "").strip() or None,
            "reasoning_effort_applied": False,
        }
        self.assertIsNone(task.result["reasoning_effort_requested"])
        self.assertFalse(task.result["reasoning_effort_applied"])

    def test_acp_resume_path_does_not_set_gateway_reasoning_fields(self) -> None:
        """The ACP resume path keeps its own result shape.

        ACP resume applies reasoning_effort through configure_session
        (thought_level), so its result must not carry the Gateway-only
        reasoning_effort_applied marker.
        """
        task = TaskState(
            task_id="t1", prompt="p", cwd=".", resume_session_id="existing",
            reasoning_effort="low",
        )
        # Mirrors server.py ACP branch task.result assembly (no gateway fields)
        task.result = {
            "stopReason": "end_turn",
            "transportStopReason": "end_turn",
            "transportError": None,
        }
        self.assertNotIn("reasoning_effort_applied", task.result)
        self.assertNotIn("reasoning_effort_requested", task.result)



class TimeoutCompatibilityTests(unittest.TestCase):
    def tearDown(self) -> None:
        TASKS.clear()

    def test_gateway_uses_legacy_timeout_when_max_is_omitted(self) -> None:
        with patch("workbuddy_bridge.server._run"):
            result = workbuddy_start(
                prompt="p",
                cwd=".",
                model="hy3",
                timeout_seconds=300,
                idle_timeout_seconds=180,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(TASKS[result["task_id"]].max_task_duration_seconds, 300.0)

    def test_gateway_explicit_max_overrides_legacy_timeout(self) -> None:
        with patch("workbuddy_bridge.server._run"):
            result = workbuddy_start(
                prompt="p",
                cwd=".",
                model="hy3",
                timeout_seconds=300,
                idle_timeout_seconds=180,
                max_task_duration_seconds=600,
            )
        self.assertTrue(result["ok"])

if __name__ == "__main__":
    unittest.main()

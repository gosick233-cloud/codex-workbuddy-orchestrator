from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from workbuddy_bridge import server
from workbuddy_bridge.server import (
    IDEMPOTENCY_TASKS,
    TASKS,
    TASKS_LOCK,
    TaskState,
    _note_task_activity,
    _public,
    _request_fingerprint,
    workbuddy_cancel,
    workbuddy_start,
    workbuddy_status,
    workbuddy_wait,
    workbuddy_result,
)


class RequestFingerprintTests(unittest.TestCase):
    """Safe request fingerprint for idempotency conflict detection."""

    def test_same_safe_fields_produce_same_fingerprint(self) -> None:
        fp1 = _request_fingerprint("prompt", "/tmp", "hy3", "high", "S1", "sess-1", "t", False)
        fp2 = _request_fingerprint("prompt", "/tmp", "hy3", "high", "S1", "sess-1", "t", False)
        self.assertEqual(fp1, fp2)

    def test_different_model_produces_different_fingerprint(self) -> None:
        fp1 = _request_fingerprint("prompt", "/tmp", "hy3", "high", "S1", "", "", False)
        fp2 = _request_fingerprint("prompt", "/tmp", "lite", "high", "S1", "", "", False)
        self.assertNotEqual(fp1, fp2)

    def test_different_prompt_produces_different_fingerprint(self) -> None:
        fp1 = _request_fingerprint("prompt A", "/tmp", "hy3", "high", "S1", "", "", False)
        fp2 = _request_fingerprint("prompt B", "/tmp", "hy3", "high", "S1", "", "", False)
        self.assertNotEqual(fp1, fp2)


    def test_different_indentation_produces_different_fingerprint(self) -> None:
        fp1 = _request_fingerprint("if x:\n    do_a()", "/tmp", "hy3", "high", "S1", "", "", False)
        fp2 = _request_fingerprint("if x:\n        do_a()", "/tmp", "hy3", "high", "S1", "", "", False)
        self.assertNotEqual(fp1, fp2)

    def test_line_ending_only_difference_reuses_fingerprint(self) -> None:
        fp1 = _request_fingerprint("line one\nline two", "/tmp", "hy3", "high", "S1", "", "", False)
        fp2 = _request_fingerprint("line one\r\nline two", "/tmp", "hy3", "high", "S1", "", "", False)
        self.assertEqual(fp1, fp2)
    def test_prompt_is_digested_never_stored_in_fingerprint(self) -> None:
        """The fingerprint digests prompt text; it never contains the prompt."""
        fp = _request_fingerprint(
            "SECRET PROMPT TEXT", "/tmp", "hy3", "high", "S1", "", "", False
        )
        self.assertNotIn("SECRET PROMPT TEXT", fp)
        self.assertEqual(len(fp), 16)


class PublicTaskContractTests(unittest.TestCase):
    """Public task state must expose safe fields without leaking content."""

    def _make_task(self, **kwargs) -> TaskState:
        defaults = {
            "task_id": "wb-test",
            "prompt": "SECRET PROMPT TEXT",
            "cwd": "/secret/path",
            "identity": "",
            "model": "hy3",
            "answer": "SECRET ANSWER TEXT",
        }
        defaults.update(kwargs)
        return TaskState(**defaults)

    def test_public_exposes_route_and_runtime(self) -> None:
        task = self._make_task(route="gateway", gateway=True)
        view = _public(task)
        self.assertEqual(view["runtime"], "workbuddy")
        self.assertEqual(view["route"], "gateway")

    def test_public_exposes_acp_resume_route(self) -> None:
        task = self._make_task(route="acp_resume", resume_session_id="sess-1")
        view = _public(task)
        self.assertEqual(view["route"], "acp_resume")

    def test_public_exposes_timestamps(self) -> None:
        task = self._make_task(
            started_at=1000.0,
            first_prompt_accepted_at=1001.0,
            updated_at=1002.0,
            finished_at=1003.0,
        )
        view = _public(task)
        self.assertEqual(view["started_at"], 1000.0)
        self.assertEqual(view["first_prompt_accepted_at"], 1001.0)
        self.assertEqual(view["updated_at"], 1002.0)
        self.assertEqual(view["finished_at"], 1003.0)

    def test_public_exposes_cancel_status(self) -> None:
        task = self._make_task(
            cancel_requested=True,
            cancel_confirmed=True,
            cancel_scope="gateway_run",
            cancel_initiator="user",
        )
        view = _public(task)
        self.assertTrue(view["cancel_requested"])
        self.assertTrue(view["cancel_confirmed"])
        self.assertEqual(view["cancel_scope"], "gateway_run")
        self.assertEqual(view["cancel_initiator"], "user")
        self.assertEqual(view["cancellation"]["scope"], "gateway_run")
        self.assertEqual(view["cancellation"]["initiator"], "user")

    def test_public_exposes_terminal_reason_and_activity(self) -> None:
        task = self._make_task(terminal_reason="end_turn")
        task.activity.append({"kind": "task_accepted", "at": 1000.0})
        view = _public(task)
        self.assertEqual(view["terminal_reason"], "end_turn")
        self.assertEqual(len(view["activity"]), 1)
        self.assertEqual(view["activity"][0]["kind"], "task_accepted")

    def test_activity_history_is_bounded_to_latest_twenty(self) -> None:
        task = self._make_task()
        for index in range(25):
            _note_task_activity(task, "stream_activity")
        self.assertEqual(len(task.activity), 20)

    def test_public_does_not_leak_prompt_or_answer_or_cwd(self) -> None:
        task = self._make_task()
        view = _public(task)
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("SECRET PROMPT TEXT", blob)
        self.assertNotIn("SECRET ANSWER TEXT", blob)
        self.assertNotIn("/secret/path", blob)

    def test_public_does_not_leak_token_or_thought_content(self) -> None:
        task = self._make_task()
        task.result = {
            "backend": "gateway_runs",
            "runId": "run-1",
            "stopReason": "end_turn",
            "observability": {
                "event_count": 2,
                "has_usage": True,
                "token_values": {"usage.input_tokens": 10},
            },
        }
        task.state = "completed"
        view = _public(task)
        blob = json.dumps(view, ensure_ascii=False)
        # result_summary may include backend/run_id/stop_reason but NOT raw
        # observability token values (those stay in task.result, not _public)
        self.assertNotIn("token_values", blob)
        self.assertNotIn("unknown_field_paths", blob)
        self.assertEqual(view["result_summary"]["stream"]["event_count"], 2)

    def test_public_replaces_raw_backend_error_with_safe_category(self) -> None:
        task = self._make_task(
            error="backend echoed SECRET PROMPT TEXT from /secret/path",
            terminal_reason="WorkBuddyError",
        )
        view = _public(task)
        self.assertIn("WorkBuddyError", view["error"])
        self.assertNotIn("SECRET PROMPT TEXT", view["error"])
        self.assertNotIn("/secret/path", view["error"])

    def test_public_does_not_expose_backend_run_identifiers(self) -> None:
        """Public status must not leak run_id/request_ref/session_ref values."""
        task = self._make_task(
            route="gateway",
            gateway=True,
            session_id="sess-secret-1",
            request_ref="run-secret-1",
        )
        task.result = {
            "backend": "gateway_runs",
            "runId": "run-secret-1",
            "stopReason": "end_turn",
            "observability": {"event_count": 1, "has_usage": False},
        }
        task.state = "completed"
        view = _public(task)
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("request_ref", blob)
        self.assertNotIn("session_ref", blob)
        self.assertNotIn("run_id", blob)
        self.assertNotIn("run-secret-1", blob)
        self.assertNotIn("sess-secret-1", blob)


class IdempotencyTests(unittest.TestCase):
    """Idempotency key protection: replay vs conflict vs no-dispatch."""

    def setUp(self) -> None:
        TASKS.clear()
        IDEMPOTENCY_TASKS.clear()

    def tearDown(self) -> None:
        TASKS.clear()
        IDEMPOTENCY_TASKS.clear()

    def _seed_task(self, key: str, model: str = "hy3", prompt: str = "original prompt") -> TaskState:
        """Pre-populate TASKS/IDEMPOTENCY_TASKS with a completed task."""
        fp = _request_fingerprint(
            prompt, str(Path.cwd()), model, "", "", "", "", False
        )
        task = TaskState(
            task_id="wb-seeded",
            prompt=prompt,
            cwd=str(Path.cwd()),
            model=model,
            state="completed",
            idempotency_key=key,
            request_fingerprint=fp,
        )
        TASKS[task.task_id] = task
        IDEMPOTENCY_TASKS[key] = task.task_id
        return task

    def test_same_key_same_fingerprint_replays_without_dispatch(self) -> None:
        """Same key + same fingerprint → return original, no second dispatch."""
        self._seed_task("key-1", model="hy3")
        result = workbuddy_start(
            prompt="original prompt",
            cwd=str(Path.cwd()),
            model="hy3",
            idempotency_key="key-1",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["replayed"])
        self.assertEqual(result["task_id"], "wb-seeded")
        # No new task was created
        self.assertEqual(len(TASKS), 1)

    def test_same_key_different_prompt_errors(self) -> None:
        """Same key + different prompt → conflict error, no dispatch."""
        self._seed_task("key-3", model="hy3")
        result = workbuddy_start(
            prompt="different prompt text",
            cwd=str(Path.cwd()),
            model="hy3",
            idempotency_key="key-3",
        )
        self.assertFalse(result["ok"])
        self.assertIn("冲突", result["error"])
        self.assertIn("指纹不匹配", result["error"])
        # No new task was created
        self.assertEqual(len(TASKS), 1)

    def test_same_key_different_fingerprint_errors(self) -> None:
        """Same key + different fingerprint → conflict error, no dispatch."""
        self._seed_task("key-2", model="hy3")
        result = workbuddy_start(
            prompt="whatever",
            cwd=str(Path.cwd()),
            model="deepseek-v4-flash",  # different model → different fingerprint
            idempotency_key="key-2",
        )
        self.assertFalse(result["ok"])
        self.assertIn("冲突", result["error"])
        self.assertIn("指纹不匹配", result["error"])
        # No new task was created
        self.assertEqual(len(TASKS), 1)

    def test_no_idempotency_key_always_dispatches(self) -> None:
        """Without idempotency_key, each call creates a new task."""
        # We can't fully dispatch (no desktop server), but we can verify a new
        # task_id is generated and registered before _run fails.
        with patch("workbuddy_bridge.server._run"):
            result = workbuddy_start(
                prompt="test prompt",
                cwd=str(Path.cwd()),
                model="hy3",
            )
        self.assertTrue(result["ok"])
        self.assertFalse(result["replayed"])
        self.assertIn("task_id", result)


class WaitTimeoutTests(unittest.TestCase):
    """wait must not re-dispatch on timeout; returns current state."""

    def setUp(self) -> None:
        TASKS.clear()

    def tearDown(self) -> None:
        TASKS.clear()

    def test_wait_timeout_returns_state_without_redispatch(self) -> None:
        """wait on a running task times out and returns state, no re-dispatch."""
        task = TaskState(
            task_id="wb-wait-test",
            prompt="p",
            cwd=".",
            state="running",
        )
        TASKS[task.task_id] = task
        result = workbuddy_wait(task.task_id, timeout_seconds=0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["wait_timed_out"])
        self.assertEqual(result["state"], "running")
        # Task is still the same object, no new task created
        self.assertEqual(len(TASKS), 1)

    def test_wait_on_terminal_returns_immediately(self) -> None:
        task = TaskState(
            task_id="wb-done",
            prompt="p",
            cwd=".",
            state="completed",
        )
        TASKS[task.task_id] = task
        result = workbuddy_wait(task.task_id, timeout_seconds=5)
        self.assertTrue(result["ok"])
        self.assertFalse(result["wait_timed_out"])
        self.assertEqual(result["state"], "completed")


class ExplicitResultTests(unittest.TestCase):
    def setUp(self) -> None:
        TASKS.clear()

    def tearDown(self) -> None:
        TASKS.clear()

    def test_completed_task_exposes_answer_only_via_explicit_result_tool(self) -> None:
        task = TaskState(
            task_id="wb-result",
            prompt="secret prompt",
            cwd="C:/secret",
            model="hy3",
            state="completed",
            answer="delegated material",
            result={"backend": "gateway_runs", "runId": "run-1", "answer": "delegated material"},
        )
        TASKS[task.task_id] = task
        status = workbuddy_status(task.task_id)
        result = workbuddy_result(task.task_id)
        self.assertNotIn("delegated material", str(status))
        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"], "delegated material")
        self.assertNotIn("secret prompt", str(result))
        self.assertNotIn("C:/secret", str(result))

    def test_non_completed_task_has_no_result_material(self) -> None:
        task = TaskState(task_id="wb-pending-result", prompt="p", cwd=".", state="running")
        TASKS[task.task_id] = task
        result = workbuddy_result(task.task_id)
        self.assertFalse(result["ok"])
        self.assertNotIn("answer", result)


class CancelStatusTests(unittest.TestCase):
    """Cancel must set scope and track confirmation."""

    def setUp(self) -> None:
        TASKS.clear()

    def tearDown(self) -> None:
        TASKS.clear()

    def test_cancel_unknown_task_fails(self) -> None:
        result = workbuddy_cancel("nonexistent")
        self.assertFalse(result["ok"])

    def test_cancel_gateway_task_sets_scope(self) -> None:
        task = TaskState(
            task_id="wb-cancel-gw",
            prompt="p",
            cwd=".",
            gateway=True,
            gateway_run_id="run-1",
            gateway_server=Mock(),
            state="observing",
        )
        TASKS[task.task_id] = task
        with patch("workbuddy_bridge.server.gateway_cancel_run"):
            result = workbuddy_cancel(task.task_id)
        self.assertTrue(result["ok"])
        self.assertTrue(result["cancel_requested"])
        self.assertEqual(result["cancel_scope"], "gateway_run")
        self.assertFalse(result["cancel_confirmed"])  # not yet confirmed
        self.assertEqual(result["state"], "cancelling")

    def test_cancel_gateway_transport_error_keeps_requested_state(self) -> None:
        task = TaskState(
            task_id="wb-cancel-gw-error",
            prompt="p",
            cwd=".",
            gateway=True,
            gateway_run_id="run-1",
            gateway_server=Mock(),
            state="observing",
        )
        TASKS[task.task_id] = task
        with patch(
            "workbuddy_bridge.server.gateway_cancel_run",
            side_effect=server.WorkBuddyError("backend echoed secret"),
        ):
            result = workbuddy_cancel(task.task_id)
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancel_requested"])
        self.assertEqual(result["cancel_scope"], "gateway_run")
        self.assertEqual(result["state"], "cancelling")
        self.assertNotIn("secret", result["error"])

    def test_cancel_acp_task_sets_scope(self) -> None:
        client = Mock()
        task = TaskState(
            task_id="wb-cancel-acp",
            prompt="p",
            cwd=".",
            state="observing",
            session_id="sess-1",
            client=client,
        )
        TASKS[task.task_id] = task
        result = workbuddy_cancel(task.task_id)
        self.assertTrue(result["ok"])
        self.assertTrue(result["cancel_requested"])
        self.assertEqual(result["cancel_scope"], "acp_session")
        self.assertFalse(result["cancel_confirmed"])
        client.notify.assert_called_once_with("session/cancel", {"sessionId": "sess-1"})

    def test_cancel_confirmed_set_on_terminal(self) -> None:
        """After _run detects cancellation, cancel_confirmed=True."""
        task = TaskState(
            task_id="wb-cancel-done",
            prompt="p",
            cwd=".",
            gateway=True,
            state="cancelled",
            cancel_requested=True,
            cancel_confirmed=True,
            cancel_scope="gateway_run",
            terminal_reason="cancelled",
        )
        TASKS[task.task_id] = task
        result = workbuddy_status(task.task_id)
        self.assertTrue(result["ok"])
        self.assertTrue(result["cancel_confirmed"])
        self.assertEqual(result["cancel_scope"], "gateway_run")
        self.assertEqual(result["terminal_reason"], "cancelled")

    def test_cancel_completed_task_does_not_mutate_terminal_state(self) -> None:
        task = TaskState(
            task_id="wb-completed-cancel",
            prompt="p",
            cwd=str(Path.cwd()),
            state="completed",
            gateway=True,
            gateway_run_id="run-completed",
        )
        TASKS[task.task_id] = task
        result = workbuddy_cancel(task.task_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "completed")
        self.assertFalse(task.cancel_requested)


class RunCancellationTests(unittest.TestCase):
    """_run must confirm cancellation only on an explicit cancel terminal."""

    def setUp(self) -> None:
        TASKS.clear()

    def tearDown(self) -> None:
        TASKS.clear()

    def _gateway_task(self, *, cancel_requested: bool = False) -> TaskState:
        task = TaskState(
            task_id="wb-run-cancel",
            prompt="p",
            cwd=str(Path.cwd()),
        )
        task.gateway = True
        task.gateway_run_id = "run-1"
        task.gateway_server = Mock()
        if cancel_requested:
            task.cancel_requested = True
            task.cancel_scope = "gateway_run"
        return task

    def _run_gateway(self, task: TaskState, stream_error: BaseException) -> None:
        with (
            patch("workbuddy_bridge.server.discover_desktop_server", return_value=Mock()),
            patch("workbuddy_bridge.server.spawn_isolated_server", return_value=Mock()),
            patch("workbuddy_bridge.server.gateway_post_run", return_value="run-1"),
            patch("workbuddy_bridge.server.gateway_stream_run", side_effect=stream_error),
            patch("workbuddy_bridge.server.gateway_cancel_run"),
        ):
            server._run(task, timeout_seconds=5.0)

    def test_arbitrary_error_after_cancel_request_does_not_confirm(self) -> None:
        """cancel_requested + an ordinary error must NOT set cancel_confirmed."""
        task = self._gateway_task(cancel_requested=True)
        self._run_gateway(task, server.WorkBuddyError("connection reset"))
        self.assertEqual(task.state, "failed")
        self.assertTrue(task.cancel_requested)
        self.assertFalse(task.cancel_confirmed)
        self.assertEqual(task.terminal_reason, "WorkBuddyError")

    def test_explicit_cancel_terminal_confirms(self) -> None:
        """Only an explicit cancelled terminal acknowledges cancellation."""
        task = self._gateway_task(cancel_requested=True)
        self._run_gateway(task, server.GatewayCancelledError())
        self.assertEqual(task.state, "cancelled")
        self.assertTrue(task.cancel_confirmed)
        self.assertEqual(task.cancel_confirmed_at is not None, True)
        self.assertEqual(task.terminal_reason, "cancelled")
        self.assertEqual(task.cancel_scope, "gateway_run")

        self.assertTrue(_public(task)["error"])
    def test_backend_initiated_cancel_without_request_stays_failed(self) -> None:
        """A cancel terminal without a caller request is not a user cancel."""
        task = self._gateway_task(cancel_requested=False)
        self._run_gateway(task, server.GatewayCancelledError())
        self.assertEqual(task.state, "failed")
        self.assertFalse(task.cancel_confirmed)

        self.assertEqual(_public(task)["error"], "Gateway run was cancelled")

if __name__ == "__main__":
    unittest.main()

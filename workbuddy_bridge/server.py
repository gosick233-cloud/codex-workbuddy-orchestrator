from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from workbuddy_bridge.activity_log import ActivityLogger
from workbuddy_bridge.acp import (
    AcpClient,
    GatewayCancelledError,
    GatewayTimeoutError,
    WorkBuddyError,
    _gateway_event_is_activity,
    discover_desktop_server,
    gateway_cancel_run,
    gateway_post_run,
    gateway_stream_run,
    kill_isolated_server,
    spawn_isolated_server,
    task_prompt,
)
from workbuddy_bridge.history import register_completed_session, wait_for_task_registration
from workbuddy_bridge.identities import compose_identity_prompt, normalize_identity
from workbuddy_bridge.multiplexer import SessionEventChannel
from workbuddy_bridge.review_sessions import (
    REVIEW_IDENTITIES,
    bind_review_session,
    build_rereview_prompt,
    find_review_session,
    normalize_review_target,
    normalize_session_id,
    prepare_review_resume,
    target_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "work" / "workbuddy-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP(
    "workbuddy_worker",
    instructions="Delegate bounded tasks to the currently running WorkBuddy desktop agent.",
    log_level="WARNING",
)


@dataclass
class TaskState:
    task_id: str
    prompt: str
    cwd: str
    identity: str = ""
    model: str = ""
    reasoning_effort: str = ""
    resume_session_id: str = ""
    review_target: str = ""
    resume_review: bool = False
    resumed: bool = False
    previous_sha256: str | None = None
    current_sha256: str | None = None
    state: str = "queued"
    session_id: str | None = None
    answer: str = ""
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    client: AcpClient | None = None
    channel: SessionEventChannel | None = None
    # Gateway single-request path (new sessions only): one POST /api/v1/runs
    # creates the session and runs the first prompt, avoiding a separate ACP
    # session/new.  Resume/review-resume keep the ACP path.
    gateway: bool = False
    gateway_run_id: str = ""
    gateway_server: Any = None
    # Public, content-free observability.  These fields deliberately never
    # contain prompt/answer text, paths, credentials, token counts, or model
    # reasoning data.
    route: str = ""
    request_ref: str | None = None
    first_prompt_accepted_at: float | None = None
    terminal_reason: str | None = None
    activity: list[dict[str, Any]] = field(default_factory=list)
    idempotency_key: str = ""
    # Safe request fingerprint for idempotency conflict detection.  Computed
    # from non-sensitive routing fields (cwd, model, identity, resume_session_id,
    # review_target, resume_review, reasoning_effort) — never from prompt text.
    request_fingerprint: str = ""
    # Cancel tracking: cancel_requested = caller asked; cancel_confirmed = the
    # backend/process acknowledged termination; cancel_scope = what was cancelled.
    cancel_confirmed: bool = False
    cancel_scope: str = ""  # "gateway_run" / "acp_session" / "process"
    cancel_initiator: str = ""  # "user" / "diagnostic"
    cancel_requested_at: float | None = None
    cancel_confirmed_at: float | None = None
    idle_timeout_seconds: float = 180.0
    max_task_duration_seconds: float = 1800.0
    first_activity_at: float | None = None
    last_activity_at: float | None = None
    last_activity_kind: str | None = None
    event_count: int = 0
    timeout_reason: str | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)


TASKS: dict[str, TaskState] = {}
TASKS_LOCK = threading.Lock()
IDEMPOTENCY_TASKS: dict[str, str] = {}
PROMPT_DISPATCH_INTERVAL_SECONDS = 1.0
PROMPT_DISPATCH_LOCK = threading.Lock()
LAST_PROMPT_DISPATCH_AT = 0.0
CONTINUATION_SESSION_LOCKS: dict[str, threading.Lock] = {}
CONTINUATION_SESSION_LOCKS_LOCK = threading.Lock()

# Gateway hosts boot directly with the real task model (one request creates
# the session and runs the first prompt); ACP hosts keep the historical
# default model for new sessions.
WORKBUDDY_DEFAULT_TASK_MODEL = "deepseek-v4-flash"


@dataclass
class PromptTransport:
    done: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str = ""
    thread: threading.Thread | None = None


def _dispatch_prompt(
    task: TaskState,
    client: AcpClient,
    event_callback: Any,
    prompt: str,
) -> PromptTransport:
    """Atomically create and start one session before releasing the shared host."""
    global LAST_PROMPT_DISPATCH_AT
    transport = PromptTransport()

    def send() -> None:
        try:
            response = client.prompt(
                prompt,
                session_id=task.session_id,
                event_callback=task.channel.feed,
            )
            transport.response = response
            transport.result = response["result"]
        except Exception as exc:
            transport.error = str(exc)
        finally:
            transport.done.set()

    with PROMPT_DISPATCH_LOCK:
        if task.resume_session_id:
            task.session_id = client.load_session(
                task.resume_session_id,
                task.cwd,
            )
        else:
            task.session_id = client.new_session(task.cwd)
        task.channel = SessionEventChannel(
            task.session_id,
            event_callback=event_callback,
        )
        client.configure_session(
            model=task.model,
            reasoning_effort=task.reasoning_effort,
            permission_mode="fullAccess",
            session_id=task.session_id,
        )
        now = time.monotonic()
        delay = PROMPT_DISPATCH_INTERVAL_SECONDS - (now - LAST_PROMPT_DISPATCH_AT)
        if delay > 0:
            time.sleep(delay)
        LAST_PROMPT_DISPATCH_AT = time.monotonic()
        task.state = "running"
        task.started_at = time.time()
        transport.thread = threading.Thread(
            target=send,
            name=f"workbuddy-prompt-{task.task_id}",
            daemon=True,
        )
        transport.thread.start()
        if not task.channel.wait_for_prompt_start(15.0):
            raise WorkBuddyError(
                f"WorkBuddy did not accept prompt for session: {task.session_id}"
            )
    return transport


def _gateway_task_model(task: TaskState) -> str:
    """Startup model for the single-request gateway path (new sessions).

    The run inherits the isolated host's startup model, so the host boots
    with the real task model directly — no separate ACP session/new.
    """
    return task.model or WORKBUDDY_DEFAULT_TASK_MODEL


def _request_fingerprint(
    prompt: str,
    cwd: str,
    model: str,
    reasoning_effort: str,
    identity: str,
    resume_session_id: str,
    review_target: str,
    resume_review: bool,
) -> str:
    """Compute a safe request fingerprint for idempotency conflict detection.

    Routing fields plus a SHA-256 digest of the normalized prompt text (never
    the prompt itself).  Two calls with the same idempotency_key but different
    fingerprints indicate a conflict and must be rejected rather than silently
    returning the original task.
    """
    import hashlib

    normalized_prompt = str(prompt).replace("\r\n", "\n").replace("\r", "\n")
    payload = "|".join(
        [
            str(cwd),
            str(model.strip()),
            str(reasoning_effort.strip()),
            str(identity),
            str(resume_session_id),
            str(review_target),
            str(resume_review),
            hashlib.sha256(normalized_prompt.encode("utf-8")).hexdigest(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _note_task_activity(task: TaskState, kind: str) -> None:
    """Record an allow-listed, content-free public task activity event."""
    if kind not in {
        "task_accepted", "session_created", "prompt_accepted", "running",
        "final_response", "cancel_requested", "process_cancel_requested",
        "process_terminated", "stream_activity", "failed",
    }:
        return
    now = time.time()
    task.activity.append({"kind": kind, "at": now})
    # Keep public status bounded for long-running tasks.
    if len(task.activity) > 20:
        task.activity = task.activity[-20:]
    task.updated_at = now
    if task.first_activity_at is None:
        task.first_activity_at = now
    task.last_activity_at = now
    task.last_activity_kind = kind
    task.event_count += 1


def _result_summary(task: TaskState) -> dict[str, Any] | None:
    """Expose terminal transport facts without returning model content/data."""
    if not isinstance(task.result, dict):
        return None
    result = task.result
    summary = {
        "backend": result.get("backend") or task.route or None,
        "stop_reason": result.get("stopReason") or task.terminal_reason,
        "reasoning_effort_applied": result.get("reasoning_effort_applied"),
    }
    observability = result.get("observability")
    if isinstance(observability, dict):
        # Deliberately expose only content-free transport shape. Token values,
        # unknown field names, prompts, answers, and thoughts stay private.
        summary["stream"] = {
            key: observability.get(key)
            for key in (
                "event_count", "status_counts", "type_counts", "has_usage",
                "has_token", "has_thought", "has_reasoning", "thought_event_count",
                "has_stream_chunk", "has_final_content",
                "usage_without_recognized_tokens",
            )
            if key in observability
        }
        if isinstance(observability.get("sse_diagnostics"), dict):
            summary["sse_diagnostics"] = {
                key: int(value)
                for key, value in observability["sse_diagnostics"].items()
                if key in {
                    "raw_line_count", "sse_frame_count", "parsed_event_count",
                    "dropped_event_count", "ignored_event_count",
                } and isinstance(value, int)
            }
    return {key: value for key, value in summary.items() if value is not None}


def _safe_public_error(error: str | None, terminal_reason: str | None) -> str | None:
    """Return a diagnostic category without exposing backend exception content."""
    if not error:
        return None
    category = terminal_reason or "runtime_error"
    if terminal_reason == "GatewayCancelledError":
        return "Gateway run was cancelled"
    return f"{category}; inspect the local Bridge activity log using task_id"


def _public(task: TaskState) -> dict[str, Any]:
    """Return the stable, safe task-state contract for MCP callers."""
    return {
        "task_id": task.task_id,
        "state": task.state,
        "runtime": "workbuddy",
        "route": task.route or ("gateway" if task.gateway else "acp_resume" if task.resume_session_id else None),
        "model": task.model or None,
        "resumed": task.resumed,
        "started_at": task.started_at,
        "first_prompt_accepted_at": task.first_prompt_accepted_at,
        "updated_at": task.updated_at,
        "finished_at": task.finished_at,
        "activity": list(task.activity),
        "terminal_reason": task.terminal_reason,
        "cancel_requested": task.cancel_requested,
        "cancel_confirmed": task.cancel_confirmed,
        "cancel_scope": task.cancel_scope or None,
        "cancel_initiator": task.cancel_initiator or None,
        "cancellation": {
            "requested_at": task.cancel_requested_at,
            "scope": task.cancel_scope or None,
            "initiator": task.cancel_initiator or None,
            "confirmed_at": task.cancel_confirmed_at,
            "confirmed": task.cancel_confirmed,
        },
        "first_activity_at": task.first_activity_at,
        "last_activity_at": task.last_activity_at,
        "last_activity_kind": task.last_activity_kind,
        "event_count": task.event_count,
        "idle_timeout_seconds": task.idle_timeout_seconds,
        "max_task_duration_seconds": task.max_task_duration_seconds,
        "timeout_reason": task.timeout_reason,
        "result_summary": _result_summary(task) if task.state == "completed" else None,
        "error": _safe_public_error(task.error, task.terminal_reason),
    }


def _run(task: TaskState, timeout_seconds: float) -> None:
    log_path = LOG_DIR / f"{task.task_id}.jsonl"
    activity_logger = ActivityLogger(log_path, task.cwd, task_id=task.task_id)

    def log_event(event: dict[str, Any]) -> None:
        activity_logger.feed(event)
        task.updated_at = time.time()
        if _gateway_event_is_activity(event):
            _note_task_activity(task, "stream_activity")

    client: AcpClient | None = None
    runtime = None
    continuation_session_lock: threading.Lock | None = None
    try:
        task.state = "connecting"
        _note_task_activity(task, "running")
        activity_logger.record(
            {
                "activity": "任务已开始",
                "status": "connecting",
            }
        )
        prompt_body = compose_identity_prompt(task.identity, task.prompt)
        if task.resume_session_id:
            with CONTINUATION_SESSION_LOCKS_LOCK:
                continuation_session_lock = CONTINUATION_SESSION_LOCKS.setdefault(
                    task.resume_session_id,
                    threading.Lock(),
                )
            if not continuation_session_lock.acquire(blocking=False):
                raise WorkBuddyError(
                    f"旧会话 {task.resume_session_id} 正在执行另一轮续接任务"
                )
            task.resumed = True
            review_resume = prepare_review_resume(
                session_id=task.resume_session_id,
                identity=task.identity,
                cwd=task.cwd,
                target=task.review_target,
            )
            task.previous_sha256 = review_resume.previous_sha256
            task.current_sha256 = review_resume.current_sha256
            prompt_body = build_rereview_prompt(review_resume, task.prompt)
        elif task.review_target:
            task.current_sha256 = target_sha256(task.review_target)

        desktop = discover_desktop_server()
        if task.resume_session_id:
            # --- ACP path: resume an existing session (session/load + prompt) ---
            task.route = "acp_resume"
            runtime = spawn_isolated_server(
                desktop,
                task.cwd,
                model=task.model or WORKBUDDY_DEFAULT_TASK_MODEL,
                session_id=task.resume_session_id,
            )
            client = AcpClient(runtime, timeout_seconds=timeout_seconds)
            client.connect()
            task.client = client
            transport = _dispatch_prompt(
                task,
                client,
                log_event,
                task_prompt(prompt_body, task.cwd),
            )
            task.first_prompt_accepted_at = time.time()
            _note_task_activity(task, "session_created")
            _note_task_activity(task, "prompt_accepted")
            # ``timeout_seconds`` controls the ACP client/transport calls.
            # The task's absolute execution budget is shared with Gateway so
            # the two routes do not silently impose different task limits.
            deadline = time.monotonic() + task.max_task_duration_seconds

            task.state = "observing"
            stop_reason = ""
            while time.monotonic() < deadline:
                stop_reason = task.channel.wait_for_end(
                    min(0.25, max(0.0, deadline - time.monotonic()))
                )
                if stop_reason:
                    break
                if transport.done.is_set():
                    if transport.error:
                        raise WorkBuddyError(transport.error)
                    transport_stop_reason = str(
                        (transport.result or {}).get("stopReason") or ""
                    )
                    if transport_stop_reason:
                        stop_reason = transport_stop_reason
                        break
            if not stop_reason:
                raise WorkBuddyError(
                    f"Timed out waiting for session_end: {task.session_id}"
                )
            if stop_reason == "cancelled":
                task.state = "cancelled"
                task.error = "WorkBuddy cancelled the prompt"
                task.terminal_reason = "cancelled"
                task.cancel_confirmed = True
                task.cancel_confirmed_at = time.time()
                if not task.cancel_scope:
                    task.cancel_scope = "acp_session"
                _note_task_activity(task, "process_terminated")
                return
            if stop_reason != "end_turn":
                raise WorkBuddyError(
                    f"WorkBuddy session ended with stopReason={stop_reason}"
                )

            transport.done.wait(5.0)
            task.result = transport.result or {"transportError": transport.error}
            transport_stop_reason = str((task.result or {}).get("stopReason") or "")
            task.result = {
                **(task.result or {}),
                "stopReason": stop_reason,
                "transportStopReason": transport_stop_reason or None,
                "transportError": transport.error or None,
            }
            task.terminal_reason = stop_reason
            title = task.channel.wait_for_title() or str(
                (transport.response or {}).get("title") or ""
            )
            answer = task.channel.answer() or str(
                (transport.response or {}).get("answer") or ""
            )
        else:
            # --- Gateway path: single POST /api/v1/runs (create + first prompt) ---
            # Boots the host directly with the real model, so one request both
            # creates the session and runs the first prompt.  Note: per-run
            # reasoning_effort is not applied on this path (model fixed at boot).
            runtime = spawn_isolated_server(
                desktop,
                task.cwd,
                model=_gateway_task_model(task),
            )
            task.gateway = True
            task.route = "gateway"
            task.gateway_server = runtime
            task.state = "running"
            task.started_at = time.time()
            run_id = gateway_post_run(runtime, task_prompt(prompt_body, task.cwd))
            task.gateway_run_id = run_id
            task.request_ref = run_id
            task.first_prompt_accepted_at = time.time()
            _note_task_activity(task, "prompt_accepted")
            task.state = "observing"
            try:
                gateway_result = gateway_stream_run(
                    runtime,
                    run_id,
                    timeout_seconds=task.max_task_duration_seconds,
                    idle_timeout_seconds=task.idle_timeout_seconds,
                    max_task_duration_seconds=task.max_task_duration_seconds,
                    event_callback=log_event,
                )
            except WorkBuddyError as exc:
                # Cancellation is confirmed only when the Gateway stream
                # terminally acknowledged it (GatewayCancelledError).  Any
                # other error keeps cancel_confirmed=False even when the
                # caller requested cancellation: a transport/stream failure
                # must not be misreported as an acknowledged cancellation.
                if task.cancel_requested and isinstance(exc, GatewayCancelledError):
                    task.state = "cancelled"
                    task.error = "WorkBuddy cancelled the prompt"
                    task.terminal_reason = "cancelled"
                    task.cancel_confirmed = True
                    task.cancel_confirmed_at = time.time()
                    if not task.cancel_scope:
                        task.cancel_scope = "gateway_run"
                    _note_task_activity(task, "process_terminated")
                    return
                raise
            task.session_id = gateway_result["session_id"]
            _note_task_activity(task, "session_created")
            task.result = {
                **gateway_result["result"],
                "backend": "gateway_runs",
                # Gateway new sessions do not apply per-request reasoning_effort:
                # the model is fixed at Host boot via --model, so the requested
                # value is recorded as requested (not applied) to avoid echoing
                # the input as effective.
                "reasoning_effort_requested": (task.reasoning_effort or "").strip() or None,
                "reasoning_effort_applied": False,
                "observability": gateway_result.get("observability"),
            }
            task.terminal_reason = str(
                gateway_result["result"].get("stopReason") or "end_turn"
            )
            title = gateway_result.get("title") or ""
            answer = gateway_result.get("answer") or ""
            activity_logger.record(
                {
                    "activity": "任务已完成",
                    "status": "end_turn",
                    "session_id": task.session_id or None,
                }
            )

        if task.session_id and not wait_for_task_registration(task.session_id):
            register_completed_session(
                task.session_id,
                task.cwd,
                generated_title=title,
            )
        task.answer = answer
        if task.review_target and task.identity in REVIEW_IDENTITIES:
            bind_review_session(
                session_id=task.session_id,
                identity=task.identity,
                cwd=task.cwd,
                target=task.review_target,
                baseline_sha256=task.current_sha256
                or target_sha256(task.review_target),
            )
        task.state = "completed"
        _note_task_activity(task, "final_response")
    except Exception as exc:
        # A timeout/stream failure must actively cancel the remote Gateway run.
        # Local Host teardown alone does not prove that the cloud-side run has
        # stopped.  Keep cancel_confirmed false until a backend terminal signal
        # is observed; this is diagnostic cancellation, not a retry/fallback.
        if (
            task.gateway
            and task.gateway_run_id
            and task.gateway_server is not None
            and not task.cancel_requested
        ):
            task.cancel_requested = True
            task.cancel_scope = "gateway_run"
            task.cancel_initiator = "diagnostic"
            task.cancel_requested_at = time.time()
            try:
                gateway_cancel_run(task.gateway_server, task.gateway_run_id)
                _note_task_activity(task, "process_cancel_requested")
            except Exception:
                _note_task_activity(task, "cancel_requested")
        task.state = "failed"
        task.error = str(exc)
        task.terminal_reason = type(exc).__name__
        if isinstance(exc, GatewayTimeoutError):
            task.timeout_reason = exc.timeout_reason
        _note_task_activity(task, "failed")
        activity_logger.terminal(
            "任务执行失败",
            status=type(exc).__name__,
            session_id=task.session_id,
        )
    finally:
        if client:
            client.close()
        if runtime:
            try:
                kill_isolated_server(runtime)
            except Exception:
                pass
        if continuation_session_lock and continuation_session_lock.locked():
            continuation_session_lock.release()
        activity_logger.close()
        task.client = None
        task.channel = None
        task.updated_at = time.time()
        task.finished_at = task.updated_at
        with task.condition:
            task.condition.notify_all()


@mcp.tool()
def workbuddy_status(task_id: str = "") -> dict[str, Any]:
    """Check WorkBuddy desktop connectivity, or inspect one dispatched task."""
    if task_id:
        with TASKS_LOCK:
            task = TASKS.get(task_id)
        if not task:
            return {"ok": False, "error": f"Unknown task_id: {task_id}"}
        return {"ok": True, **_public(task)}
    try:
        server = discover_desktop_server()
        return {
            "ok": True,
            "connected": True,
            "endpoint": server.acp_endpoint,
            "sidecar_pid": server.sidecar_pid,
            "host_session_id": server.session_host_id,
            "max_concurrent_tasks": 4,
            "event_routing": "isolated_runtime_per_task",
        }
    except Exception as exc:
        return {"ok": False, "connected": False, "error": str(exc)}


@mcp.tool()
def workbuddy_start(
    prompt: str,
    cwd: str = "",
    timeout_seconds: int = 300,
    model: str = "",
    reasoning_effort: str = "",
    identity: str = "",
    resume_session_id: str = "",
    review_target: str = "",
    resume_review: bool = False,
    idempotency_key: str = "",
    idle_timeout_seconds: int = 180,
    max_task_duration_seconds: int | None = None,
) -> dict[str, Any]:
    """Queue a task once, or replay a previously accepted idempotency key.

    The key is caller-supplied and never sent to WorkBuddy.  Replaying it only
    reads the original task state; it cannot issue a second Gateway request.
    """
    working_dir = Path(cwd or Path.cwd()).resolve()
    if not working_dir.is_dir():
        return {"ok": False, "error": f"cwd is not a directory: {working_dir}"}
    if not prompt.strip():
        return {"ok": False, "error": "prompt must not be empty"}
    effective_max_task_duration = (
        max_task_duration_seconds
        if max_task_duration_seconds is not None
        else timeout_seconds
    )
    if idle_timeout_seconds <= 0 or effective_max_task_duration <= 0:
        return {"ok": False, "error": "timeouts must be positive"}
    if idle_timeout_seconds >= effective_max_task_duration:
        return {"ok": False, "error": "idle_timeout_seconds must be less than max_task_duration_seconds"}
    canonical_idempotency_key = idempotency_key.strip()
    if len(canonical_idempotency_key) > 128:
        return {"ok": False, "error": "idempotency_key must be at most 128 characters"}
    try:
        canonical_identity = normalize_identity(identity)
        canonical_resume_session_id = (
            normalize_session_id(resume_session_id)
            if resume_session_id.strip()
            else ""
        )
        canonical_review_target = (
            normalize_review_target(review_target, str(working_dir))
            if review_target.strip()
            else ""
        )
        if resume_review and canonical_identity not in REVIEW_IDENTITIES:
            raise ValueError("resume_review 只能与 S1、S2、S3 一起使用")
        if resume_review and not canonical_resume_session_id:
            if not canonical_review_target:
                raise ValueError("自动复审时必须提供 review_target")
            canonical_resume_session_id = find_review_session(
                canonical_identity,
                str(working_dir),
                canonical_review_target,
            )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if canonical_resume_session_id:
        if canonical_identity not in REVIEW_IDENTITIES:
            return {
                "ok": False,
                "error": "只有 S1、S2、S3 支持复用旧审查会话",
            }
        if not canonical_review_target:
            return {
                "ok": False,
                "error": "复用旧审查会话时必须提供 review_target",
            }
    if canonical_review_target and canonical_identity not in REVIEW_IDENTITIES:
        return {
            "ok": False,
            "error": "review_target 只能与 S1、S2、S3 审查身份一起使用",
        }
    fingerprint = _request_fingerprint(
        prompt,
        str(working_dir),
        model,
        reasoning_effort,
        canonical_identity,
        canonical_resume_session_id,
        canonical_review_target,
        bool(canonical_resume_session_id and canonical_identity in REVIEW_IDENTITIES),
    )
    if canonical_idempotency_key:
        with TASKS_LOCK:
            existing_id = IDEMPOTENCY_TASKS.get(canonical_idempotency_key)
            existing = TASKS.get(existing_id) if existing_id else None
        if existing:
            # Same key + same fingerprint → replay (return original, no dispatch).
            # Same key + different fingerprint → conflict, must error.
            if existing.request_fingerprint != fingerprint:
                return {
                    "ok": False,
                    "error": (
                        f"idempotency_key 冲突：该 key 已绑定另一个请求"
                        f"（指纹不匹配）。不得用同一 key 派发不同请求。"
                    ),
                }
            return {"ok": True, "replayed": True, **_public(existing)}

    task_id = f"wb-{uuid.uuid4().hex[:12]}"
    task = TaskState(
        task_id=task_id,
        prompt=prompt,
        cwd=str(working_dir),
        identity=canonical_identity,
        model=model.strip(),
        reasoning_effort=reasoning_effort.strip(),
        resume_session_id=canonical_resume_session_id,
        review_target=canonical_review_target,
        resume_review=bool(
            canonical_resume_session_id
            and canonical_identity in REVIEW_IDENTITIES
        ),
        idempotency_key=canonical_idempotency_key,
        request_fingerprint=fingerprint,
        idle_timeout_seconds=float(idle_timeout_seconds),
        max_task_duration_seconds=float(effective_max_task_duration),
    )
    with TASKS_LOCK:
        # A concurrent replay may have won between the initial lookup and this
        # insert.  In that case return its task instead of dispatching again.
        if canonical_idempotency_key:
            existing_id = IDEMPOTENCY_TASKS.get(canonical_idempotency_key)
            existing = TASKS.get(existing_id) if existing_id else None
            if existing:
                if existing.request_fingerprint != fingerprint:
                    return {
                        "ok": False,
                        "error": (
                            f"idempotency_key 冲突：该 key 已绑定另一个请求"
                            f"（指纹不匹配）。不得用同一 key 派发不同请求。"
                        ),
                    }
                return {"ok": True, "replayed": True, **_public(existing)}
        TASKS[task_id] = task
        if canonical_idempotency_key:
            IDEMPOTENCY_TASKS[canonical_idempotency_key] = task_id
    _note_task_activity(task, "task_accepted")
    thread = threading.Thread(target=_run, args=(task, float(timeout_seconds)), daemon=True)
    thread.start()
    return {
        "ok": True,
        "task_id": task_id,
        "replayed": False,
        **_public(task),
    }


@mcp.tool()
def workbuddy_wait(task_id: str, timeout_seconds: int = 55) -> dict[str, Any]:
    """Wait briefly for a WorkBuddy task; returns current state on timeout."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return {"ok": False, "error": f"Unknown task_id: {task_id}"}
    wait_timed_out = False
    if task.state not in {"completed", "failed", "cancelled"}:
        with task.condition:
            task.condition.wait(timeout=max(0, min(timeout_seconds, 55)))
        wait_timed_out = task.state not in {"completed", "failed", "cancelled"}
    return {"ok": True, "wait_timed_out": wait_timed_out, **_public(task)}


@mcp.tool()
def workbuddy_result(task_id: str) -> dict[str, Any]:
    """Return final subagent material only after a task completed successfully.

    Status and wait responses intentionally remain content-free. Callers
    authorized to consume delegated work use this explicit terminal endpoint.
    """
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return {"ok": False, "error": f"Unknown task_id: {task_id}"}
    if task.state != "completed":
        return {
            "ok": False,
            "error": "Final subagent material is not available for this task state",
            **_public(task),
        }
    return {
        "ok": True,
        "task_id": task.task_id,
        "state": task.state,
        "runtime": "workbuddy",
        "model": task.model or None,
        "answer": task.answer,
        "result_summary": _result_summary(task),
    }


@mcp.tool()
def workbuddy_cancel(task_id: str) -> dict[str, Any]:
    """Request cancellation of a running WorkBuddy task.

    Sets ``cancel_requested`` immediately and ``cancel_scope`` to indicate what
    was targeted (``gateway_run`` or ``acp_session``).  ``cancel_confirmed`` is
    set later by ``_run`` when the backend/process acknowledges termination.
    """
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return {"ok": False, "error": f"Unknown task_id: {task_id}"}
    if task.state in {"completed", "failed", "cancelled"}:
        return {"ok": False, "state": task.state, "error": "Task already finished"}
    if task.gateway:
        if not task.gateway_run_id or task.gateway_server is None:
            return {"ok": False, "state": task.state, "error": "Task is not cancellable right now"}
        # Record the caller's cancellation intent before the transport call.
        # A connection-reset response can still mean the host received and
        # applied the cancellation, so it must not erase this observability
        # fact.
        task.cancel_requested = True
        task.cancel_scope = "gateway_run"
        task.cancel_initiator = "user"
        task.cancel_requested_at = time.time()
        task.state = "cancelling"
        _note_task_activity(task, "process_cancel_requested")
        try:
            gateway_cancel_run(task.gateway_server, task.gateway_run_id)
            return {"ok": True, **_public(task)}
        except WorkBuddyError as exc:
            return {
                "ok": False,
                **_public(task),
                "error": "Gateway cancellation transport could not be confirmed; inspect the local Bridge activity log using task_id",
            }
    client = task.client
    if not client or not task.session_id:
        return {"ok": False, "state": task.state, "error": "Task is not cancellable right now"}
    try:
        task.cancel_requested = True
        task.cancel_scope = "acp_session"
        task.cancel_initiator = "user"
        task.cancel_requested_at = time.time()
        task.state = "cancelling"
        _note_task_activity(task, "cancel_requested")
        client.notify("session/cancel", {"sessionId": task.session_id})
        return {"ok": True, **_public(task)}
    except WorkBuddyError as exc:
        return {
            "ok": False,
            **_public(task),
            "error": "ACP cancellation transport could not be confirmed; inspect the local Bridge activity log using task_id",
        }


@mcp.tool()
def workbuddy_list() -> dict[str, Any]:
    """List tasks known to this bridge process."""
    with TASKS_LOCK:
        tasks = list(TASKS.values())
    return {"ok": True, "tasks": [_public(task) for task in tasks]}


if __name__ == "__main__":
    mcp.run(transport="stdio")

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from workbuddy_bridge.acp import (
    AcpClient,
    DesktopServer,
    WorkBuddyError,
    _event_session_id,
    _message_text,
    _session_title,
)


EventCallback = Callable[[dict[str, Any]], None]


def _session_end_reason(event: dict[str, Any]) -> str:
    params = event.get("params") or {}
    update = params.get("update") if isinstance(params, dict) else None
    if not isinstance(update, dict) or update.get("sessionUpdate") != "session_end":
        return ""
    return str(update.get("stopReason") or "")


def _is_prompt_activity(event: dict[str, Any]) -> bool:
    params = event.get("params") or {}
    update = params.get("update") if isinstance(params, dict) else None
    if not isinstance(update, dict):
        return False
    return str(update.get("sessionUpdate") or "") in {
        "user_message_chunk",
        "agent_thought_chunk",
        "agent_message_chunk",
        "tool_call",
        "session_end",
    }


@dataclass
class SessionEventChannel:
    session_id: str
    event_callback: EventCallback | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    observer_error: str = ""
    condition: threading.Condition = field(default_factory=threading.Condition)

    def feed(self, event: dict[str, Any]) -> None:
        if _event_session_id(event) != self.session_id:
            return
        callback = self.event_callback
        with self.condition:
            self.events.append(event)
            reason = _session_end_reason(event)
            if reason:
                self.stop_reason = reason
            self.condition.notify_all()
        if callback:
            callback(event)

    def fail(self, message: str) -> None:
        with self.condition:
            self.observer_error = message
            self.condition.notify_all()

    def wait_for_end(self, timeout_seconds: float) -> str:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self.condition:
            while not self.stop_reason and not self.observer_error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            if self.observer_error:
                raise WorkBuddyError(self.observer_error)
            return self.stop_reason

    def wait_for_prompt_start(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self.condition:
            while not any(_is_prompt_activity(event) for event in self.events):
                if self.observer_error:
                    raise WorkBuddyError(self.observer_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return True

    def wait_for_title(self, timeout_seconds: float = 2.0) -> str:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self.condition:
            while True:
                title = _session_title(self.events)
                if title:
                    return title
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ""
                self.condition.wait(remaining)

    def answer(self) -> str:
        with self.condition:
            events = list(self.events)
        return "".join(filter(None, (_message_text(event) for event in events))).strip()


class AcpEventMultiplexer:
    """Observe one WorkBuddy ACP broadcast stream and route events by sessionId."""

    def __init__(self, server: DesktopServer) -> None:
        self.server = server
        self._client = AcpClient(server)
        self._channels: dict[str, SessionEventChannel] = {}
        self._channels_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    def start(self, timeout_seconds: float = 10.0) -> None:
        self._client.connect()
        self._thread = threading.Thread(
            target=self._listen,
            name="workbuddy-acp-observer",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout_seconds):
            self.close()
            raise WorkBuddyError("WorkBuddy ACP observation stream did not become ready")
        if self.error:
            raise WorkBuddyError(self.error)

    def _listen(self) -> None:
        try:
            self._client.listen(self._route, ready_event=self._ready)
            if not self._closed.is_set():
                raise WorkBuddyError("WorkBuddy ACP observation stream closed unexpectedly")
        except Exception as exc:
            if not self._closed.is_set():
                self.error = f"WorkBuddy ACP observer failed: {exc}"
                self._ready.set()
                with self._channels_lock:
                    channels = list(self._channels.values())
                for channel in channels:
                    channel.fail(self.error)

    def _route(self, event: dict[str, Any]) -> None:
        session_id = _event_session_id(event)
        if not session_id:
            return
        with self._channels_lock:
            channel = self._channels.get(session_id)
        if channel:
            channel.feed(event)

    def subscribe(
        self,
        session_id: str,
        *,
        event_callback: EventCallback | None = None,
    ) -> SessionEventChannel:
        channel = SessionEventChannel(session_id, event_callback=event_callback)
        with self._channels_lock:
            if session_id in self._channels:
                raise WorkBuddyError(f"Session is already observed: {session_id}")
            self._channels[session_id] = channel
        return channel

    def unsubscribe(self, session_id: str) -> None:
        with self._channels_lock:
            self._channels.pop(session_id, None)

    def close(self) -> None:
        self._closed.set()
        self._client.close()


_MULTIPLEXER: AcpEventMultiplexer | None = None
_MULTIPLEXER_LOCK = threading.Lock()


def shared_multiplexer(server: DesktopServer) -> AcpEventMultiplexer:
    global _MULTIPLEXER
    with _MULTIPLEXER_LOCK:
        current = _MULTIPLEXER
        if current and not current.error and current.server.acp_endpoint == server.acp_endpoint:
            return current
        if current:
            current.close()
        current = AcpEventMultiplexer(server)
        current.start()
        _MULTIPLEXER = current
        return current

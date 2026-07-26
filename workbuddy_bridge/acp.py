from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx


REQUEST_HEADER = {"x-codebuddy-request": "1"}
HOST_SESSION_PREFIX = "__workbuddy_cli_host__"
DEFAULT_TIMEOUT_SECONDS = 600.0


class WorkBuddyError(RuntimeError):
    """Raised when the WorkBuddy desktop bridge cannot complete a request."""


def task_prompt(prompt: str, cwd: str) -> str:
    """Give a WorkBuddy Task access context while its native cwd stays empty."""
    return f"工作目录（需要访问文件时使用此绝对路径）：{Path(cwd).resolve()}\n\n{prompt}"


@dataclass(frozen=True)
class DesktopServer:
    acp_endpoint: str
    password: str
    session_host_id: str
    sidecar_pipe: str
    sidecar_pid: int
    runtime_pid: int = 0

    @property
    def base_url(self) -> str:
        suffix = "/api/v1/acp"
        if not self.acp_endpoint.endswith(suffix):
            raise WorkBuddyError(f"Unexpected ACP endpoint: {self.acp_endpoint}")
        return self.acp_endpoint[: -len(suffix)]


def _rpc(pipe_path: str, method: str, params: dict[str, Any] | None = None) -> Any:
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    try:
        with open(pipe_path, "r+b", buffering=0) as pipe:
            pipe.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            raw = pipe.readline()
    except OSError as exc:
        raise WorkBuddyError(f"Cannot contact WorkBuddy sidecar: {exc}") from exc
    if not raw:
        raise WorkBuddyError("WorkBuddy sidecar returned an empty response")
    response = json.loads(raw.decode("utf-8"))
    if "error" in response:
        error = response["error"]
        raise WorkBuddyError(error.get("message", str(error)))
    return response.get("result")


def _candidate_sidecars() -> list[tuple[Path, str, int]]:
    temp_root = Path(os.environ.get("TEMP", "")) / "wb"
    candidates: list[tuple[Path, str, int]] = []
    if not temp_root.is_dir():
        return candidates
    for pid_file in temp_root.glob("*/sidecar.pid"):
        try:
            payload = json.loads(pid_file.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            runtime_id = pid_file.parent.name
            pipe = rf"\\.\pipe\workbuddy-{runtime_id}-sidecar-control"
            candidates.append((pid_file, pipe, pid))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    candidates.sort(key=lambda item: item[0].stat().st_mtime, reverse=True)
    return candidates


def discover_desktop_server() -> DesktopServer:
    """Discover the live WorkBuddy desktop ACP host and its rotating password."""
    errors: list[str] = []
    for _, pipe, sidecar_pid in _candidate_sidecars():
        try:
            sessions = _rpc(pipe, "session.list") or []
            hosts = [
                item
                for item in sessions
                if str(item.get("sessionId", "")).startswith(HOST_SESSION_PREFIX)
                and item.get("acpEndpoint")
            ]
            if not hosts:
                errors.append(f"sidecar {sidecar_pid}: desktop ACP host not found")
                continue
            host = hosts[0]
            captured = _rpc(
                pipe,
                "session.capture",
                {"sessionId": host["sessionId"], "lines": 300},
            )
            text = str((captured or {}).get("text", ""))
            # The URL is printed as an OSC-8 terminal hyperlink. Stop before
            # either BEL or ESC terminators instead of treating them as part
            # of the rotating password.
            match = re.search(r"[?&]password=([^\s\x00-\x1f&]+)", text)
            if not match:
                errors.append(f"sidecar {sidecar_pid}: password not present in host output")
                continue
            password = urllib.parse.unquote(match.group(1))
            server = DesktopServer(
                acp_endpoint=str(host["acpEndpoint"]),
                password=password,
                session_host_id=str(host["sessionId"]),
                sidecar_pipe=pipe,
                sidecar_pid=sidecar_pid,
                runtime_pid=int(host.get("pid") or 0),
            )
            with httpx.Client(timeout=5.0, trust_env=False) as client:
                response = client.get(
                    f"{server.base_url}/api/v1/auth/status",
                    headers={**REQUEST_HEADER, "Authorization": f"Bearer {password}"},
                )
                response.raise_for_status()
                status = response.json()
                if status.get("authEnabled") and not status.get("authenticated"):
                    raise WorkBuddyError("Discovered WorkBuddy password was rejected")
            return server
        except Exception as exc:  # keep trying other live sidecars
            errors.append(f"sidecar {sidecar_pid}: {exc}")
    detail = "; ".join(errors) if errors else "no WorkBuddy sidecar PID files found"
    raise WorkBuddyError(f"WorkBuddy desktop server discovery failed: {detail}")


def _process_executable(pid: int) -> Path:
    if os.name != "nt" or pid <= 0:
        raise WorkBuddyError("WorkBuddy runtime executable discovery requires Windows")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        raise WorkBuddyError(f"Cannot inspect WorkBuddy runtime process: {pid}")
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            raise WorkBuddyError(f"Cannot resolve WorkBuddy executable: {pid}")
        return Path(buffer.value).resolve()
    finally:
        kernel32.CloseHandle(process)


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def spawn_isolated_server(
    desktop: DesktopServer,
    cwd: str,
    *,
    model: str = "deepseek-v4-flash",
    startup_timeout_seconds: float = 30.0,
    session_id: str = "",
) -> DesktopServer:
    """Start one WorkBuddy CLI host whose ACP state is private to one task."""
    executable = _process_executable(desktop.runtime_pid)
    cli_path = executable.parent / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"
    if not cli_path.is_file():
        raise WorkBuddyError(f"WorkBuddy CLI entry was not found: {cli_path}")
    runtime_id = f"codex-worker-{uuid.uuid4().hex[:12]}"
    port = _free_local_port()
    config_dir = Path(
        os.environ.get("WORKBUDDY_CONFIG_DIR")
        or os.environ.get("CODEBUDDY_CONFIG_DIR")
        or Path.home() / ".workbuddy"
    ).expanduser().resolve()
    params = {
        "sessionId": runtime_id,
        "command": str(executable),
        "args": [
            str(cli_path),
            "--serve",
            "--setting-sources",
            "user",
            "--permission-mode",
            "fullAccess",
            "--permission-mode-before-plan",
            "bypassPermissions",
            "--model",
            model or "deepseek-v4-flash",
            *(["--session-id", session_id] if session_id else []),
        ],
        "cwd": str(Path(cwd).resolve()),
        "port": port,
        "env": {
            "ELECTRON_RUN_AS_NODE": "1",
            "CODEBUDDY_DISABLE_IDE": "1",
            "CODEBUDDY_CODE_DISABLE_TERMINAL_TITLE": "1",
            "CODEBUDDY_CONFIG_DIR": str(config_dir),
        },
        "cols": 80,
        "rows": 24,
    }
    created = _rpc(desktop.sidecar_pipe, "session.create", params)
    endpoint = str((created or {}).get("acpEndpoint") or f"http://127.0.0.1:{port}/api/v1/acp")
    runtime = DesktopServer(
        endpoint,
        "",
        runtime_id,
        desktop.sidecar_pipe,
        desktop.sidecar_pid,
        int((created or {}).get("pid") or 0),
    )
    deadline = time.monotonic() + startup_timeout_seconds
    try:
        with httpx.Client(timeout=1.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    response = client.get(
                        f"{runtime.base_url}/api/v1/auth/status",
                        headers=REQUEST_HEADER,
                    )
                    if response.is_success:
                        return runtime
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
    except Exception:
        kill_isolated_server(runtime)
        raise
    kill_isolated_server(runtime)
    raise WorkBuddyError(f"Isolated WorkBuddy host did not become ready: {runtime_id}")


def kill_isolated_server(server: DesktopServer) -> None:
    if not server.session_host_id.startswith("codex-worker-"):
        return
    try:
        _rpc(server.sidecar_pipe, "session.kill", {"sessionId": server.session_host_id})
    except WorkBuddyError as exc:
        if "Session not found" not in str(exc):
            raise


def _iter_sse_json(response: httpx.Response):
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                payload = "".join(data_lines)
                data_lines.clear()
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    pass
            continue
        if line.startswith("data: "):
            data_lines.append(line[6:])
        elif line.startswith("{"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass
    if data_lines:
        try:
            yield json.loads("".join(data_lines))
        except json.JSONDecodeError:
            pass


def _message_text(update: dict[str, Any]) -> str:
    params = update.get("params") or {}
    body = params.get("update") if isinstance(params, dict) else None
    if not isinstance(body, dict):
        body = params if isinstance(params, dict) else {}
    kind = str(body.get("sessionUpdate", body.get("type", ""))).lower()
    if "agent_message" not in kind and kind not in {"assistant_message", "message_chunk"}:
        return ""
    content = body.get("content")
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    if isinstance(body.get("text"), str):
        return body["text"]
    return ""


def _event_session_id(event: dict[str, Any]) -> str:
    params = event.get("params") or {}
    if not isinstance(params, dict):
        return ""
    return str(params.get("sessionId") or params.get("session_id") or "")


def _session_events(
    events: list[dict[str, Any]], session_id: str
) -> list[dict[str, Any]]:
    """Keep broadcasts belonging to one ACP session."""
    return [event for event in events if _event_session_id(event) == session_id]


def _session_title(events: list[dict[str, Any]]) -> str:
    """Return the final title generated by WorkBuddy for an ACP session."""
    for event in reversed(events):
        params = event.get("params") or {}
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") != "session_info_update":
            continue
        title = update.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return ""


class AcpClient:
    def __init__(
        self,
        server: DesktopServer,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.server = server
        self.timeout_seconds = timeout_seconds
        self.event_callback = event_callback
        self.connection_id: str | None = None
        self.session_token: str | None = None
        self.session_id: str | None = None
        self.is_playground = False
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._http = httpx.Client(timeout=None, trust_env=False)
        self._send_lock = threading.Lock()

    def close(self) -> None:
        if self.connection_id:
            try:
                self._http.delete(self.server.acp_endpoint, headers=self._headers())
            except Exception:
                pass
        self._http.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            **REQUEST_HEADER,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.server.password:
            headers["Authorization"] = f"Bearer {self.server.password}"
        if self.connection_id:
            headers["acp-connection-id"] = self.connection_id
        if self.session_token:
            headers["acp-session-token"] = self.session_token
        return headers

    def connect(self) -> None:
        response = self._http.post(
            f"{self.server.base_url}/api/v1/acp/connect",
            headers={
                **REQUEST_HEADER,
                **(
                    {"Authorization": f"Bearer {self.server.password}"}
                    if self.server.password
                    else {}
                ),
            },
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        self.connection_id = payload["connectionId"]
        self.session_token = payload["sessionToken"]
        initialized = self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "codex-workbuddy-bridge", "version": "0.1.0"},
                "clientCapabilities": {},
            },
            timeout_seconds=30.0,
        )
        if isinstance(initialized, dict) and initialized.get("connectionId"):
            self.connection_id = str(initialized["connectionId"])

    def new_session(self, cwd: str, *, as_task: bool = True) -> str:
        # WorkBuddy's own TaskStarter creates top-level Tasks as a playground
        # session with an empty backend cwd. The original project cwd remains
        # bridge-side context and callers should use absolute paths in prompts.
        self.is_playground = as_task
        result = self.request(
            "session/new",
            {
                "cwd": "" if as_task else str(Path(cwd).resolve()),
                "mcpServers": [],
                "_meta": {
                    "codebuddy.ai": {
                        "welcomeMode": "working" if as_task else "coding",
                        "isPlayground": as_task,
                    }
                },
            },
            timeout_seconds=60.0,
        )
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise WorkBuddyError(f"WorkBuddy did not return a session ID: {result!r}")
        self.session_id = str(result["sessionId"])
        return self.session_id

    def load_session(self, session_id: str, cwd: str, *, as_task: bool = True) -> str:
        self.is_playground = as_task
        result = self.request(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": "" if as_task else str(Path(cwd).resolve()),
                "mcpServers": [],
                "_meta": {
                    "codebuddy.ai": {
                        "welcomeMode": "working" if as_task else "coding",
                        "isPlayground": as_task,
                    }
                },
            },
            timeout_seconds=60.0,
        )
        if isinstance(result, dict):
            loaded_session_id = str(result.get("sessionId") or session_id)
            if loaded_session_id != session_id:
                raise WorkBuddyError(
                    "WorkBuddy loaded an unexpected session: "
                    f"{loaded_session_id} != {session_id}"
                )
        self.session_id = session_id
        return session_id

    def set_config_option(
        self,
        config_id: str,
        value: str,
        *,
        session_id: str | None = None,
    ) -> Any:
        target_session_id = session_id or self.session_id
        if not target_session_id:
            raise WorkBuddyError("No ACP session has been created")
        return self.request(
            "session/set_config_option",
            {
                "sessionId": target_session_id,
                "configId": config_id,
                "value": value,
            },
            timeout_seconds=30.0,
        )

    def configure_session(
        self,
        *,
        model: str = "",
        reasoning_effort: str = "",
        permission_mode: str = "fullAccess",
        session_id: str | None = None,
    ) -> None:
        """Apply WorkBuddy session options before the first prompt."""
        if permission_mode.strip():
            self.set_config_option(
                "mode", permission_mode.strip(), session_id=session_id
            )
        if model.strip():
            self.set_config_option("model", model.strip(), session_id=session_id)
        if reasoning_effort.strip():
            self.set_config_option(
                "thought_level", reasoning_effort.strip(), session_id=session_id
            )

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        result_found = False
        result: Any = None
        deadline = time.monotonic() + (timeout_seconds or self.timeout_seconds)
        with self._send_lock:
            with self._http.stream(
                "POST",
                self.server.acp_endpoint,
                headers=self._headers(),
                json=payload,
                timeout=httpx.Timeout(timeout_seconds or self.timeout_seconds, connect=10.0),
            ) as response:
                response.raise_for_status()
                for event in _iter_sse_json(response):
                    if self.event_callback:
                        self.event_callback(event)
                    if event_callback and event_callback is not self.event_callback:
                        event_callback(event)
                    if event.get("id") == request_id and "method" not in event:
                        if event.get("error"):
                            error = event["error"]
                            raise WorkBuddyError(error.get("message", str(error)))
                        result = event.get("result")
                        result_found = True
                    elif event.get("id") is not None and event.get("method") == "session/request_permission":
                        self._grant_permission(event)
                    if time.monotonic() > deadline:
                        raise WorkBuddyError(f"ACP request timed out: {method}")
        if not result_found:
            raise WorkBuddyError(f"ACP response did not contain a result for {method}")
        return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send an ACP notification, which intentionally has no JSON-RPC result."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        with httpx.Client(timeout=15.0, trust_env=False) as client:
            client.post(self.server.acp_endpoint, headers=self._headers(), json=payload).raise_for_status()

    def listen(
        self,
        event_callback: Callable[[dict[str, Any]], None],
        *,
        ready_event: threading.Event | None = None,
    ) -> None:
        """Consume the connection's long-lived ACP broadcast stream."""
        if not self.connection_id:
            raise WorkBuddyError("ACP client is not connected")
        with self._http.stream(
            "GET",
            self.server.acp_endpoint,
            headers=self._headers(),
            timeout=httpx.Timeout(None, connect=10.0),
        ) as response:
            response.raise_for_status()
            if ready_event:
                ready_event.set()
            for event in _iter_sse_json(response):
                event_callback(event)

    def _grant_permission(self, event: dict[str, Any]) -> None:
        options = (event.get("params") or {}).get("options") or []
        allow = next(
            (
                item.get("optionId") or item.get("name")
                for item in options
                if str(item.get("kind") or "").lower() == "allow_always"
                or str(item.get("optionId") or "").lower() == "allow_always"
            ),
            None,
        )
        if allow is None:
            allow = next(
                (
                    item.get("optionId") or item.get("name")
                    for item in options
                    if str(item.get("kind") or "").lower() == "allow_once"
                    or str(item.get("optionId") or "").lower() == "allow"
                ),
                None,
            )
        if allow is None:
            raise WorkBuddyError("WorkBuddy permission request has no allow option")
        payload = {
            "jsonrpc": "2.0",
            "id": event["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": allow}},
        }
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            client.post(self.server.acp_endpoint, headers=self._headers(), json=payload).raise_for_status()

    def prompt(
        self,
        text: str,
        *,
        session_id: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        target_session_id = session_id or self.session_id
        if not target_session_id:
            raise WorkBuddyError("No ACP session has been created")
        events: list[dict[str, Any]] = []

        def collect(event: dict[str, Any]) -> None:
            events.append(event)
            if event_callback:
                event_callback(event)

        result = self.request(
            "session/prompt",
            {
                "sessionId": target_session_id,
                "prompt": [{"type": "text", "text": text}],
                "_meta": {
                    "codebuddy.ai": {
                        "isPlayground": self.is_playground,
                        "conversationId": target_session_id,
                        "clientSendTime": int(time.time() * 1000),
                    }
                },
            },
            event_callback=collect,
        )
        own_events = _session_events(events, target_session_id)
        answer = "".join(
            filter(None, (_message_text(event) for event in own_events))
        ).strip()
        return {
            "session_id": target_session_id,
            "answer": answer,
            "title": _session_title(own_events),
            "result": result,
            "events": own_events,
        }


def ask_desktop(
    prompt: str,
    cwd: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    model: str = "",
    reasoning_effort: str = "",
) -> dict[str, Any]:
    from workbuddy_bridge.history import register_completed_session, wait_for_task_registration

    server = discover_desktop_server()
    client = AcpClient(server, timeout_seconds=timeout_seconds)
    try:
        client.connect()
        client.new_session(cwd)
        client.configure_session(
            model=model,
            reasoning_effort=reasoning_effort,
            permission_mode="fullAccess",
        )
        response = client.prompt(task_prompt(prompt, cwd))
        stop_reason = str(response["result"].get("stopReason", ""))
        if stop_reason == "cancelled":
            raise WorkBuddyError("WorkBuddy cancelled the prompt")
        if not wait_for_task_registration(response["session_id"]):
            register_completed_session(
                response["session_id"],
                cwd,
                generated_title=response["title"],
            )
        return response
    finally:
        client.close()

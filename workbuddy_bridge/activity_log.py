from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


_READ_TOOLS = {"read", "openfile", "open_file", "view"}
_SEARCH_TOOLS = {"grep", "search", "glob", "find", "codesearch"}
_SHELL_TOOLS = {"bash", "shell", "terminal", "exec", "runcommand"}
_DEPENDENCY_TOOLS = {
    "audit",
    "dependencyscan",
    "dependency_scan",
    "npmaudit",
    "pipaudit",
}
_WEB_TOOLS = {"webfetch", "websearch", "fetch", "browser"}
_WRITE_TOOLS = {"write", "edit", "applypatch", "apply_patch", "createfile"}
_TEST_MARKERS = (
    " test",
    "test ",
    "pytest",
    "unittest",
    "npm test",
    "pnpm test",
    "yarn test",
    "vitest",
    "jest",
)
_DEPENDENCY_MARKERS = (
    "npm audit",
    "pnpm audit",
    "yarn audit",
    "pip-audit",
    "safety check",
    "dependency",
)
_MAX_DETAILS = 5
_MAX_DETAIL_LENGTH = 240


def _update(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    params = event.get("params")
    if not isinstance(params, dict):
        return {}, {}
    update = params.get("update")
    if not isinstance(update, dict):
        return params, {}
    return params, update


def _tool_name(update: dict[str, Any]) -> str:
    meta = update.get("_meta")
    if isinstance(meta, dict):
        value = meta.get("codebuddy.ai/toolName")
        if isinstance(value, str) and value.strip():
            return value.strip()
    title = update.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip().split(maxsplit=1)[0]
    kind = update.get("kind")
    return str(kind or "").strip()


def _raw_input(update: dict[str, Any]) -> dict[str, Any]:
    raw = update.get("rawInput")
    return raw if isinstance(raw, dict) else {}


def _safe_file_detail(value: Any, cwd: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    candidate = Path(value.strip())
    try:
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (cwd / candidate).resolve()
        )
        try:
            detail = str(resolved.relative_to(cwd))
        except ValueError:
            detail = resolved.name
    except (OSError, ValueError):
        detail = candidate.name
    return detail[:_MAX_DETAIL_LENGTH]


def _shell_activity(raw: dict[str, Any]) -> str:
    command = str(
        raw.get("command")
        or raw.get("cmd")
        or raw.get("script")
        or ""
    ).casefold()
    if any(marker in command for marker in _DEPENDENCY_MARKERS):
        return "正在检查依赖"
    if any(marker in f" {command} " for marker in _TEST_MARKERS):
        return "正在运行测试"
    return "正在运行验证命令"


def event_to_activity(
    event: dict[str, Any],
    cwd: str | Path,
) -> dict[str, Any] | None:
    """Convert one ACP event into a content-free activity record."""
    params, update = _update(event)
    if not update:
        return None
    session_update = str(update.get("sessionUpdate") or "")
    session_id = str(params.get("sessionId") or "")

    if session_update == "session_end":
        reason = str(update.get("stopReason") or "")
        if reason == "end_turn":
            activity = "任务已完成"
        elif reason == "cancelled":
            activity = "任务已取消"
        else:
            activity = "任务已结束"
        return {
            "activity": activity,
            "status": reason or None,
            "session_id": session_id or None,
        }

    if session_update == "tool_call_update":
        status = str(update.get("status") or "").casefold()
        if status not in {"failed", "error", "cancelled"}:
            return None
        return {
            "activity": "工具执行失败",
            "tool": _tool_name(update) or None,
            "status": status,
            "session_id": session_id or None,
        }

    if session_update != "tool_call":
        # This intentionally drops thought, answer, prompt, title, usage, and
        # all other streaming content.
        return None

    raw = _raw_input(update)
    kind = str(update.get("kind") or "").casefold()
    tool = _tool_name(update)
    normalized_tool = tool.replace("-", "").replace("_", "").casefold()
    if not raw and kind in {"", "other"}:
        # WorkBuddy commonly emits an initial placeholder and then a second,
        # detailed tool_call for the same call ID. Log only the detailed one.
        return None

    activity = "正在执行工具"
    detail = ""
    normalized_kind = kind.replace("-", "").replace("_", "")
    if normalized_kind == "read" or normalized_tool in _READ_TOOLS:
        activity = "正在读取文件"
        detail = _safe_file_detail(
            raw.get("file_path") or raw.get("path") or raw.get("file"),
            Path(cwd).resolve(),
        )
    elif normalized_kind == "search" or normalized_tool in _SEARCH_TOOLS:
        activity = "正在搜索代码"
    elif normalized_tool in _DEPENDENCY_TOOLS:
        activity = "正在检查依赖"
    elif normalized_tool in _WEB_TOOLS:
        activity = "正在访问资料"
    elif normalized_tool in _WRITE_TOOLS:
        activity = "正在修改文件"
        detail = _safe_file_detail(
            raw.get("file_path") or raw.get("path") or raw.get("file"),
            Path(cwd).resolve(),
        )
    elif normalized_tool in _SHELL_TOOLS or normalized_kind in {
        "execute",
        "terminal",
    }:
        activity = _shell_activity(raw)

    record: dict[str, Any] = {
        "activity": activity,
        "tool": tool or None,
        "session_id": session_id or None,
    }
    if detail:
        record["detail"] = detail
    return record


class ActivityLogger:
    """Write compact JSONL activity records without model or tool content."""

    def __init__(
        self,
        path: Path,
        cwd: str | Path,
        *,
        task_id: str = "",
    ) -> None:
        self.path = path
        self.cwd = Path(cwd).resolve()
        self.task_id = task_id
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._pending: dict[str, Any] | None = None
        self._pending_started_at = 0.0
        self._count = 0
        self._details: list[str] = []

    def feed(self, event: dict[str, Any]) -> None:
        record = event_to_activity(event, self.cwd)
        if record:
            self.record(record)

    def record(self, record: dict[str, Any]) -> None:
        with self._lock:
            if self._same_activity(record):
                self._count += 1
                detail = record.get("detail")
                if (
                    isinstance(detail, str)
                    and detail
                    and detail not in self._details
                    and len(self._details) < _MAX_DETAILS
                ):
                    self._details.append(detail)
                self._persist_unlocked()
                return
            self._finalize_pending_unlocked()
            self._pending = {
                key: value
                for key, value in record.items()
                if value is not None and key != "detail"
            }
            self._pending_started_at = time.time()
            self._count = 1
            detail = record.get("detail")
            self._details = [detail] if isinstance(detail, str) and detail else []
            self._persist_unlocked()

    def terminal(
        self,
        activity: str,
        *,
        status: str,
        session_id: str | None,
    ) -> None:
        self.record(
            {
                "activity": activity,
                "status": status,
                "session_id": session_id,
            }
        )
        self.close()

    def close(self) -> None:
        with self._lock:
            self._finalize_pending_unlocked()
            self._persist_unlocked()

    def _same_activity(self, record: dict[str, Any]) -> bool:
        if not self._pending:
            return False
        return (
            self._pending.get("activity") == record.get("activity")
            and self._pending.get("tool") == record.get("tool")
            and self._pending.get("session_id") == record.get("session_id")
            and "status" not in self._pending
            and not record.get("status")
        )

    def _pending_output(self) -> dict[str, Any] | None:
        if not self._pending:
            return None
        output = {
            "timestamp": self._pending_started_at,
            **({"task_id": self.task_id} if self.task_id else {}),
            **self._pending,
            "count": self._count,
        }
        if self._details:
            output["details"] = list(self._details)
        return output

    def _finalize_pending_unlocked(self) -> None:
        output = self._pending_output()
        if output:
            self._records.append(output)
        self._pending = None
        self._pending_started_at = 0.0
        self._count = 0
        self._details = []

    def _persist_unlocked(self) -> None:
        outputs = list(self._records)
        pending = self._pending_output()
        if pending:
            outputs.append(pending)
        if not outputs:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            "".join(
                json.dumps(output, ensure_ascii=False) + "\n"
                for output in outputs
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

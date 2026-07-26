from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REVIEW_IDENTITIES = frozenset({"S1", "S2", "S3"})
REGISTRY_FILENAME = "codex-review-sessions.json"
_REGISTRY_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReviewResume:
    session_id: str
    identity: str
    cwd: str
    target: str
    previous_sha256: str | None
    current_sha256: str | None
    transcript_path: Path


def normalize_session_id(session_id: str) -> str:
    value = session_id.strip()
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("resume_session_id 必须是有效的 WorkBuddy 会话 UUID") from exc


def normalize_review_target(target: str, cwd: str) -> str:
    value = target.strip()
    if not value:
        raise ValueError("review_target 不能为空")
    path = Path(value)
    if not path.is_absolute():
        path = Path(cwd) / path
    path = path.resolve()
    if not path.exists():
        raise ValueError(f"审查目标不存在: {path}")
    return str(path)


def target_sha256(target: str) -> str | None:
    path = Path(target)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def registry_path() -> Path:
    return _config_dir() / REGISTRY_FILENAME


def _config_dir() -> Path:
    return Path(
        os.environ.get("WORKBUDDY_CONFIG_DIR")
        or os.environ.get("CODEBUDDY_CONFIG_DIR")
        or Path.home() / ".workbuddy"
    ).expanduser().resolve()


def _load_registry_unlocked() -> dict[str, dict[str, Any]]:
    path = registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取复审会话注册表: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"复审会话注册表格式错误: {path}")
    sessions = data.get("sessions", {})
    if not isinstance(sessions, dict):
        raise RuntimeError(f"复审会话注册表 sessions 字段格式错误: {path}")
    return sessions


def _write_registry_unlocked(sessions: dict[str, dict[str, Any]]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {"version": 1, "sessions": sessions}
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _find_transcript(session_id: str) -> Path:
    projects = _config_dir() / "projects"
    matches = [
        path
        for path in projects.glob(f"*/{session_id}.jsonl")
        if path.is_file() and path.stat().st_size > 0
    ]
    if not matches:
        raise ValueError(
            f"找不到旧会话 {session_id} 的 WorkBuddy 对话记录，拒绝另起新会话"
        )
    if len(matches) > 1:
        raise ValueError(f"旧会话 {session_id} 存在多份对话记录，无法安全续接")
    return matches[0]


def _transcript_user_texts(path: Path) -> list[str]:
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message")
        if isinstance(message, dict):
            role = message.get("role")
            content = message.get("content", [])
        else:
            role = record.get("role")
            content = record.get("content", [])
        if role != "user":
            continue
        if isinstance(content, str):
            texts.append(content)
            continue
        if not isinstance(content, list):
            continue
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "input_text"}
        ]
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            texts.append(text)
    return texts


def bind_review_session(
    session_id: str,
    identity: str,
    cwd: str,
    target: str,
    baseline_sha256: str | None = None,
) -> None:
    session_id = normalize_session_id(session_id)
    if identity not in REVIEW_IDENTITIES:
        raise ValueError(f"{identity} 不是代码审查身份")
    normalized_cwd = str(Path(cwd).resolve())
    normalized_target = normalize_review_target(target, normalized_cwd)
    now = time.time()

    with _REGISTRY_LOCK:
        sessions = _load_registry_unlocked()
        existing = sessions.get(session_id)
        if existing:
            expected = (
                existing.get("identity"),
                str(Path(existing.get("cwd", "")).resolve()).casefold(),
                str(Path(existing.get("target", "")).resolve()).casefold(),
            )
            actual = (
                identity,
                normalized_cwd.casefold(),
                normalized_target.casefold(),
            )
            if expected != actual:
                raise ValueError(
                    f"旧会话 {session_id} 的身份、工作目录或审查目标不匹配"
                )
            created_at = existing.get("created_at", now)
        else:
            created_at = now

        sessions[session_id] = {
            "identity": identity,
            "cwd": normalized_cwd,
            "target": normalized_target,
            "baseline_sha256": baseline_sha256,
            "created_at": created_at,
            "updated_at": now,
        }
        _write_registry_unlocked(sessions)


def find_review_session(identity: str, cwd: str, target: str) -> str:
    if identity not in REVIEW_IDENTITIES:
        raise ValueError("只有 S1、S2、S3 支持复用旧审查会话")
    normalized_cwd = str(Path(cwd).resolve())
    normalized_target = normalize_review_target(target, normalized_cwd)
    with _REGISTRY_LOCK:
        sessions = _load_registry_unlocked()
    matches = [
        (session_id, binding)
        for session_id, binding in sessions.items()
        if binding.get("identity") == identity
        and str(Path(binding.get("cwd", "")).resolve()).casefold()
        == normalized_cwd.casefold()
        and str(Path(binding.get("target", "")).resolve()).casefold()
        == normalized_target.casefold()
    ]
    if not matches:
        raise ValueError(
            f"没有找到 {identity} 对该目标的旧审查会话；"
            "拒绝把“复审”悄悄改成全新审查"
        )
    matches.sort(
        key=lambda item: float(item[1].get("updated_at") or 0),
        reverse=True,
    )
    return normalize_session_id(matches[0][0])


def prepare_review_resume(
    session_id: str,
    identity: str,
    cwd: str,
    target: str,
) -> ReviewResume:
    session_id = normalize_session_id(session_id)
    if identity not in REVIEW_IDENTITIES:
        raise ValueError("只有 S1、S2、S3 支持复用旧审查会话")
    normalized_cwd = str(Path(cwd).resolve())
    normalized_target = normalize_review_target(target, normalized_cwd)
    transcript = _find_transcript(session_id)

    with _REGISTRY_LOCK:
        sessions = _load_registry_unlocked()
        existing = sessions.get(session_id)

    if existing:
        expected_identity = existing.get("identity")
        expected_cwd = str(Path(existing.get("cwd", "")).resolve())
        expected_target = str(Path(existing.get("target", "")).resolve())
        if (
            expected_identity != identity
            or expected_cwd.casefold() != normalized_cwd.casefold()
            or expected_target.casefold() != normalized_target.casefold()
        ):
            raise ValueError(
                f"旧会话 {session_id} 与 {identity} / 当前目录 / 当前目标不匹配，"
                "拒绝把复审串入错误会话"
            )
        previous_sha256 = existing.get("baseline_sha256")
    else:
        # Older sessions can be adopted once, but only when their transcript
        # proves that both the requested identity and target are correct.
        combined = "\n".join(_transcript_user_texts(transcript))
        identity_markers = (f"你是 {identity}", f"你是{identity}", f" {identity}")
        if not any(marker in combined for marker in identity_markers):
            raise ValueError(
                f"旧会话 {session_id} 的对话记录无法证明其身份为 {identity}"
            )
        if normalized_target.casefold() not in combined.casefold():
            raise ValueError(
                f"旧会话 {session_id} 的对话记录不包含当前审查目标，拒绝续接"
            )
        previous_sha256 = None
        bind_review_session(
            session_id=session_id,
            identity=identity,
            cwd=normalized_cwd,
            target=normalized_target,
            baseline_sha256=None,
        )

    return ReviewResume(
        session_id=session_id,
        identity=identity,
        cwd=normalized_cwd,
        target=normalized_target,
        previous_sha256=previous_sha256,
        current_sha256=target_sha256(normalized_target),
        transcript_path=transcript,
    )


def build_rereview_prompt(resume: ReviewResume, task_prompt: str) -> str:
    previous = resume.previous_sha256 or "未知（旧会话首次纳入绑定）"
    current = resume.current_sha256 or "不适用（目标不是单个文件）"
    return f"""这是对同一审查目标的复审。你必须重新读取当前完整目标，不能只机械核对自己上一轮提过的问题。

请严格执行两个相互独立的部分：
1. 回归检查：从本会话上一轮审查结论中提取所有问题，逐项结合当前代码证据标记为“已修复 / 部分修复 / 未修复 / 无法验证”。
2. 增量检查：暂时忽略上一轮问题清单，按照你的完整身份职责，从头审查当前完整目标，找出本次修改引入的新问题以及上一轮遗漏的问题，并与回归项去重。

输出顺序：
- 先输出回归检查结果；
- 再输出增量检查结果；
- 最后给出本轮结论；
- 每个问题必须包含当前代码位置和依据；
- 只审查，不修改任何文件。

审查目标：{resume.target}
上一轮文件 SHA-256：{previous}
当前文件 SHA-256：{current}
本轮补充要求：{task_prompt.strip() or "无"}
"""

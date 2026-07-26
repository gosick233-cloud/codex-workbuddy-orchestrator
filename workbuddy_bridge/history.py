from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from workbuddy_bridge.acp import WorkBuddyError


DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_REGISTRATION_WAIT_SECONDS = 5.0


def workbuddy_database_path() -> Path:
    """Return the desktop history database used by the current WorkBuddy profile."""
    config_dir = os.environ.get("WORKBUDDY_CONFIG_DIR") or os.environ.get(
        "CODEBUDDY_CONFIG_DIR"
    )
    if config_dir:
        return Path(config_dir).expanduser().resolve() / "workbuddy.db"
    return Path.home() / ".workbuddy" / "workbuddy.db"


def _current_user_id(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        """
        SELECT user_id
        FROM sessions
        WHERE user_id IS NOT NULL AND user_id <> ''
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row[0]) if row else ""


def wait_for_task_registration(
    session_id: str,
    *,
    database_path: str | Path | None = None,
    timeout_seconds: float = DEFAULT_REGISTRATION_WAIT_SECONDS,
) -> bool:
    """Wait for WorkBuddy to persist a session through its native task path."""
    db_path = Path(database_path) if database_path else workbuddy_database_path()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            with closing(sqlite3.connect(db_path, timeout=1.0)) as connection:
                row = connection.execute(
                    "SELECT is_playground, deleted_at FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            if row and int(row[0] or 0) == 1 and row[1] is None:
                return True
        except sqlite3.Error:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def register_completed_session(
    session_id: str,
    cwd: str,
    *,
    generated_title: str = "",
    database_path: str | Path | None = None,
    timestamp_ms: int | None = None,
) -> Path:
    """Add an ACP transcript to WorkBuddy's task history without activating its window."""
    db_path = Path(database_path) if database_path else workbuddy_database_path()
    if not db_path.is_file():
        raise WorkBuddyError(f"WorkBuddy history database was not found: {db_path}")

    now = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    canonical_cwd = str(Path(cwd).resolve())
    workbuddy_title = generated_title.strip() or None

    try:
        with closing(
            sqlite3.connect(db_path, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
        ) as connection:
            with connection:
                connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
                user_id = _current_user_id(connection)
                connection.execute(
                    """
                    INSERT INTO sessions (
                        id, cwd, user_id, title, custom_title, status,
                        created_at, updated_at, last_activity_at, deleted_at,
                        is_playground, source_mode, is_background_automation
                    ) VALUES (?, ?, ?, ?, NULL, 'completed', ?, ?, ?, NULL, 1, 'working', NULL)
                    ON CONFLICT(id) DO UPDATE SET
                        cwd = CASE
                            WHEN sessions.cwd IS NULL OR sessions.cwd = '' THEN excluded.cwd
                            ELSE sessions.cwd
                        END,
                        user_id = CASE
                            WHEN excluded.user_id <> '' THEN excluded.user_id
                            ELSE sessions.user_id
                        END,
                        title = COALESCE(sessions.title, excluded.title),
                        status = CASE
                            WHEN LOWER(sessions.status) = 'archived' THEN sessions.status
                            ELSE 'completed'
                        END,
                        updated_at = excluded.updated_at,
                        last_activity_at = excluded.last_activity_at,
                        deleted_at = NULL,
                        is_playground = 1,
                        is_background_automation = NULL
                    """,
                    (session_id, canonical_cwd, user_id, workbuddy_title, now, now, now),
                )
    except sqlite3.Error as exc:
        raise WorkBuddyError(f"Could not register WorkBuddy desktop history: {exc}") from exc
    return db_path

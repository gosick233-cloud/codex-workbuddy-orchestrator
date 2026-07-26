from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from workbuddy_bridge.history import register_completed_session, wait_for_task_registration


SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title TEXT,
    custom_title TEXT,
    status TEXT NOT NULL DEFAULT 'Pending',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    deleted_at INTEGER,
    is_playground INTEGER NOT NULL DEFAULT 0,
    source_mode TEXT,
    is_background_automation INTEGER,
    model TEXT,
    expert_id TEXT,
    expert_locale TEXT,
    expert_runtime_identity TEXT,
    expert_marketplace TEXT,
    permission_mode TEXT,
    last_activity_at INTEGER,
    use_sandbox_cli INTEGER,
    project_id TEXT,
    mode TEXT
)
"""


class HistoryRegistrationTests(unittest.TestCase):
    def test_registers_completed_regular_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "workbuddy.db"
            with closing(sqlite3.connect(db_path)) as connection:
                with connection:
                    connection.execute(SCHEMA)
                    connection.execute(
                        """
                        INSERT INTO sessions (
                            id, cwd, user_id, status, created_at, updated_at, is_playground
                        ) VALUES ('existing', ?, 'user-1', 'completed', 1, 1, 0)
                        """,
                        (temp_dir,),
                    )

            register_completed_session(
                "session-1",
                temp_dir,
                generated_title="WorkBuddy generated title",
                database_path=db_path,
                timestamp_ms=1234,
            )

            with closing(sqlite3.connect(db_path)) as connection:
                row = connection.execute(
                    """
                    SELECT user_id, title, status, created_at, updated_at,
                           last_activity_at, is_playground, is_background_automation
                    FROM sessions WHERE id = 'session-1'
                    """
                ).fetchone()

            self.assertEqual(
                row,
                (
                    "user-1",
                    "WorkBuddy generated title",
                    "completed",
                    1234,
                    1234,
                    1234,
                    1,
                    None,
                ),
            )

    def test_preserves_native_workbuddy_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "workbuddy.db"
            with closing(sqlite3.connect(db_path)) as connection:
                with connection:
                    connection.execute(SCHEMA)
                    connection.execute(
                        """
                        INSERT INTO sessions (
                            id, cwd, user_id, title, status, created_at, updated_at,
                            deleted_at, is_playground
                        ) VALUES ('session-1', 'native-cwd', 'user-1', 'Native title',
                                  'completed', 1, 1, NULL, 1)
                        """
                    )

            self.assertTrue(
                wait_for_task_registration(
                    "session-1", database_path=db_path, timeout_seconds=0
                )
            )
            register_completed_session(
                "session-1",
                temp_dir,
                generated_title="Bridge title",
                database_path=db_path,
                timestamp_ms=1234,
            )

            with closing(sqlite3.connect(db_path)) as connection:
                row = connection.execute(
                    "SELECT cwd, title FROM sessions WHERE id = 'session-1'"
                ).fetchone()
            self.assertEqual(row, ("native-cwd", "Native title"))


if __name__ == "__main__":
    unittest.main()

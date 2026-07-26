from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workbuddy_bridge.activity_log import ActivityLogger, event_to_activity


def _event(session_update: str, **update: object) -> dict[str, object]:
    return {
        "params": {
            "sessionId": "session-1",
            "update": {
                "sessionUpdate": session_update,
                **update,
            },
        }
    }


def _tool_event(
    tool: str,
    *,
    kind: str = "other",
    raw_input: dict[str, object] | None = None,
) -> dict[str, object]:
    return _event(
        "tool_call",
        title=tool,
        kind=kind,
        status="in_progress",
        rawInput=raw_input or {},
        _meta={"codebuddy.ai/toolName": tool},
    )


class ActivityEventTests(unittest.TestCase):
    def test_drops_thought_answer_prompt_and_usage_content(self) -> None:
        for kind in (
            "agent_thought_chunk",
            "agent_message_chunk",
            "user_message_chunk",
            "usage_update",
            "session_info_update",
        ):
            with self.subTest(kind=kind):
                self.assertIsNone(
                    event_to_activity(
                        _event(kind, text="sensitive model content"),
                        ".",
                    )
                )

    def test_maps_tool_types_without_copying_raw_content(self) -> None:
        cases = [
            (
                _tool_event(
                    "Read",
                    kind="read",
                    raw_input={"file_path": "src/app.py"},
                ),
                "正在读取文件",
            ),
            (
                _tool_event(
                    "Grep",
                    kind="search",
                    raw_input={"pattern": "secret query"},
                ),
                "正在搜索代码",
            ),
            (
                _tool_event(
                    "Shell",
                    kind="execute",
                    raw_input={"command": "python -m pytest"},
                ),
                "正在运行测试",
            ),
            (
                _tool_event(
                    "Shell",
                    kind="execute",
                    raw_input={"command": "npm audit"},
                ),
                "正在检查依赖",
            ),
            (
                _tool_event(
                    "WebFetch",
                    raw_input={"url": "https://example.test/private"},
                ),
                "正在访问资料",
            ),
        ]

        for event, expected in cases:
            with self.subTest(expected=expected):
                record = event_to_activity(event, ".")
                self.assertIsNotNone(record)
                self.assertEqual(record["activity"], expected)
                serialized = json.dumps(record, ensure_ascii=False)
                self.assertNotIn("secret query", serialized)
                self.assertNotIn("python -m pytest", serialized)
                self.assertNotIn("npm audit", serialized)
                self.assertNotIn("example.test", serialized)

    def test_ignores_placeholder_and_success_update_but_keeps_failure(self) -> None:
        placeholder = _tool_event("Read")
        completed = _event(
            "tool_call_update",
            status="completed",
            rawOutput="sensitive output",
        )
        failed = _event(
            "tool_call_update",
            status="failed",
            content=[{"type": "content", "text": "sensitive failure output"}],
            rawOutput="sensitive output",
            _meta={"codebuddy.ai/toolName": "Read"},
        )

        self.assertIsNone(event_to_activity(placeholder, "."))
        self.assertIsNone(event_to_activity(completed, "."))
        failure_record = event_to_activity(failed, ".")
        self.assertEqual(failure_record["activity"], "工具执行失败")
        self.assertNotIn(
            "sensitive",
            json.dumps(failure_record, ensure_ascii=False),
        )


class ActivityLoggerTests(unittest.TestCase):
    def test_coalesces_repeated_actions_and_never_writes_stream_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "task.jsonl"
            logger = ActivityLogger(path, root, task_id="wb-test")
            logger.feed(
                _event(
                    "agent_thought_chunk",
                    text="private chain of thought",
                )
            )
            logger.feed(
                _event(
                    "agent_message_chunk",
                    text="private final answer",
                )
            )
            logger.feed(
                _tool_event(
                    "Read",
                    kind="read",
                    raw_input={"file_path": str(root / "one.py")},
                )
            )
            self.assertIn(
                "正在读取文件",
                path.read_text(encoding="utf-8"),
            )
            logger.feed(
                _tool_event(
                    "Read",
                    kind="read",
                    raw_input={"file_path": str(root / "two.py")},
                )
            )
            logger.feed(
                _event("session_end", stopReason="end_turn")
            )
            logger.close()

            content = path.read_text(encoding="utf-8")
            records = [
                json.loads(line)
                for line in content.splitlines()
                if line.strip()
            ]

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["activity"], "正在读取文件")
        self.assertEqual(records[0]["count"], 2)
        self.assertEqual(records[0]["details"], ["one.py", "two.py"])
        self.assertEqual(records[1]["activity"], "任务已完成")
        self.assertNotIn("private chain of thought", content)
        self.assertNotIn("private final answer", content)


if __name__ == "__main__":
    unittest.main()

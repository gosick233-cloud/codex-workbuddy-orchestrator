from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from workbuddy_bridge.review_sessions import (
    bind_review_session,
    build_rereview_prompt,
    find_review_session,
    prepare_review_resume,
    registry_path,
    target_sha256,
)
from workbuddy_bridge.server import workbuddy_start


class ReviewSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.target = self.cwd / "app.html"
        self.target.write_text("<p>first</p>", encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {"WORKBUDDY_CONFIG_DIR": str(self.profile)},
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def _transcript(self, session_id: str, text: str) -> Path:
        path = self.profile / "projects" / "test-project" / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def test_bound_session_resumes_only_for_the_same_identity_and_target(self) -> None:
        session_id = str(uuid.uuid4())
        self._transcript(session_id, f"你是 S1\n审查 {self.target}")
        original_sha = target_sha256(str(self.target))
        bind_review_session(
            session_id,
            "S1",
            str(self.cwd),
            str(self.target),
            original_sha,
        )

        self.target.write_text("<p>second</p>", encoding="utf-8")
        resume = prepare_review_resume(
            session_id,
            "S1",
            str(self.cwd),
            str(self.target),
        )

        self.assertEqual(resume.previous_sha256, original_sha)
        self.assertEqual(resume.current_sha256, target_sha256(str(self.target)))
        with self.assertRaisesRegex(ValueError, "不匹配"):
            prepare_review_resume(
                session_id,
                "S2",
                str(self.cwd),
                str(self.target),
            )

    def test_legacy_session_can_be_adopted_from_its_persisted_prompt(self) -> None:
        session_id = str(uuid.uuid4())
        self._transcript(session_id, f"你是 S3\n审查目标：{self.target}")

        resume = prepare_review_resume(
            session_id,
            "S3",
            str(self.cwd),
            str(self.target),
        )

        self.assertEqual(resume.session_id, session_id)
        registry = json.loads(registry_path().read_text(encoding="utf-8"))
        self.assertEqual(registry["sessions"][session_id]["identity"], "S3")

    def test_legacy_session_is_rejected_when_target_is_not_in_transcript(self) -> None:
        session_id = str(uuid.uuid4())
        self._transcript(session_id, "你是 S2\n审查另一个文件")

        with self.assertRaisesRegex(ValueError, "不包含当前审查目标"):
            prepare_review_resume(
                session_id,
                "S2",
                str(self.cwd),
                str(self.target),
            )

    def test_rereview_prompt_requires_regression_and_full_incremental_review(self) -> None:
        session_id = str(uuid.uuid4())
        self._transcript(session_id, f"你是 S1\n审查 {self.target}")
        bind_review_session(
            session_id,
            "S1",
            str(self.cwd),
            str(self.target),
            target_sha256(str(self.target)),
        )
        resume = prepare_review_resume(
            session_id,
            "S1",
            str(self.cwd),
            str(self.target),
        )

        prompt = build_rereview_prompt(resume, "请复审")

        self.assertIn("回归检查", prompt)
        self.assertIn("增量检查", prompt)
        self.assertIn("从头审查当前完整目标", prompt)
        self.assertIn("不能只机械核对", prompt)

    def test_can_find_the_latest_bound_session_for_automatic_rereview(self) -> None:
        older = str(uuid.uuid4())
        newer = str(uuid.uuid4())
        bind_review_session(older, "S2", str(self.cwd), str(self.target), "old")
        bind_review_session(newer, "S2", str(self.cwd), str(self.target), "new")

        self.assertEqual(
            find_review_session("S2", str(self.cwd), str(self.target)),
            newer,
        )

    def test_resume_requires_explicit_review_target(self) -> None:
        result = workbuddy_start(
            "复审",
            cwd=str(self.cwd),
            identity="S1",
            resume_session_id=str(uuid.uuid4()),
        )

        self.assertFalse(result["ok"])
        self.assertIn("review_target", result["error"])

    def test_automatic_rereview_fails_instead_of_starting_a_new_session(self) -> None:
        result = workbuddy_start(
            "复审",
            cwd=str(self.cwd),
            identity="S3",
            review_target=str(self.target),
            resume_review=True,
        )

        self.assertFalse(result["ok"])
        self.assertIn("拒绝", result["error"])

if __name__ == "__main__":
    unittest.main()

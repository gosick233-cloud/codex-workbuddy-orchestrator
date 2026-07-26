from __future__ import annotations

import unittest

from workbuddy_bridge.multiplexer import SessionEventChannel


def event(session_id: str, update: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }


class SessionEventChannelTests(unittest.TestCase):
    def test_routes_answer_title_and_end_for_one_session(self) -> None:
        channel = SessionEventChannel("s1")
        channel.feed(
            event(
                "s2",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "wrong"},
                },
            )
        )
        channel.feed(
            event(
                "s1",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "right"},
                },
            )
        )
        channel.feed(
            event(
                "s1",
                {"sessionUpdate": "session_info_update", "title": "S1 title"},
            )
        )
        channel.feed(
            event("s1", {"sessionUpdate": "session_end", "stopReason": "end_turn"})
        )

        self.assertEqual(channel.answer(), "right")
        self.assertEqual(channel.wait_for_title(0), "S1 title")
        self.assertEqual(channel.wait_for_end(0), "end_turn")
        self.assertEqual(len(channel.events), 3)

    def test_transport_cancel_is_not_a_channel_end(self) -> None:
        channel = SessionEventChannel("s1")

        self.assertEqual(channel.wait_for_end(0), "")
        channel.feed(
            event("s1", {"sessionUpdate": "session_end", "stopReason": "end_turn"})
        )
        self.assertEqual(channel.wait_for_end(0), "end_turn")

    def test_prompt_start_requires_session_activity(self) -> None:
        channel = SessionEventChannel("s1")
        channel.feed(
            event("s1", {"sessionUpdate": "config_option_update", "options": []})
        )
        self.assertFalse(channel.wait_for_prompt_start(0))
        channel.feed(event("s1", {"sessionUpdate": "user_message_chunk"}))
        self.assertTrue(channel.wait_for_prompt_start(0))


if __name__ == "__main__":
    unittest.main()

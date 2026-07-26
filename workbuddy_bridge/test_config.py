from __future__ import annotations

import unittest
from unittest.mock import Mock

from workbuddy_bridge.acp import AcpClient


class SessionConfigTests(unittest.TestCase):
    def test_applies_model_and_reasoning_before_prompt(self) -> None:
        client = object.__new__(AcpClient)
        client.session_id = "session-1"
        client.request = Mock(return_value={})

        client.configure_session(
            model="deepseek-v4-flash",
            reasoning_effort="max",
        )

        self.assertEqual(client.request.call_count, 3)
        client.request.assert_any_call(
            "session/set_config_option",
            {
                "sessionId": "session-1",
                "configId": "mode",
                "value": "fullAccess",
            },
            timeout_seconds=30.0,
        )
        client.request.assert_any_call(
            "session/set_config_option",
            {
                "sessionId": "session-1",
                "configId": "model",
                "value": "deepseek-v4-flash",
            },
            timeout_seconds=30.0,
        )
        client.request.assert_any_call(
            "session/set_config_option",
            {
                "sessionId": "session-1",
                "configId": "thought_level",
                "value": "max",
            },
            timeout_seconds=30.0,
        )

    def test_explicit_session_does_not_use_mutable_current_session(self) -> None:
        client = object.__new__(AcpClient)
        client.session_id = "latest-session"
        client.request = Mock(return_value={})

        client.configure_session(
            model="deepseek-v4-flash",
            reasoning_effort="low",
            session_id="target-session",
        )

        for call in client.request.call_args_list:
            self.assertEqual(call.args[1]["sessionId"], "target-session")


if __name__ == "__main__":
    unittest.main()

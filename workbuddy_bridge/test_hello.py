from __future__ import annotations

import json
from pathlib import Path

from workbuddy_bridge.acp import ask_desktop


if __name__ == "__main__":
    response = ask_desktop("你好", str(Path.cwd()), timeout_seconds=180.0)
    print(
        json.dumps(
            {
                "session_id": response["session_id"],
                "answer": response["answer"],
                "result": response["result"],
                "event_count": len(response["events"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

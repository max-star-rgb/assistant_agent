from __future__ import annotations

import importlib.util
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoContext


def test_retired_context_package_is_absent() -> None:
    assert importlib.util.find_spec("assistant_agent.context") is None


def test_live_video_context_is_owned_by_video_module() -> None:
    assert RealtimeVideoContext().status == "unavailable"

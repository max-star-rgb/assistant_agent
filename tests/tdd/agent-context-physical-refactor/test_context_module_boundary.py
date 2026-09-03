from __future__ import annotations

import importlib.util
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoContext
from assistant_agent.observability.trace_query import TraceQueryService


def test_retired_context_package_is_absent() -> None:
    assert importlib.util.find_spec("assistant_agent.context") is None


def test_live_video_context_is_owned_by_video_module() -> None:
    assert RealtimeVideoContext().status == "unavailable"


def test_trace_query_exposes_only_consumed_summary_lookups() -> None:
    assert not hasattr(TraceQueryService, "tool_calls_by_run")
    assert not hasattr(TraceQueryService, "context_by_run")
    assert not hasattr(TraceQueryService, "context_by_trace")

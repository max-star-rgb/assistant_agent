from importlib import import_module

import pytest


def test_only_observability_modules_with_live_consumers_remain() -> None:
    import_module("assistant_agent.media.vision.observability")

    retired = (
        "agent_service_delivery",
        "agent_service_latency",
        "hook_dispatch",
        "langsmith_native",
        "trace_conversation",
        "trace_ledger",
        "trace_metrics",
        "trace_persistence",
        "trace_query",
        "trace_store",
        "turn_summary",
        "visual_trace_content",
    )
    for name in retired:
        with pytest.raises(ModuleNotFoundError) as exc_info:
            import_module(f"assistant_agent.observability.{name}")
        assert exc_info.value.name in {
            "assistant_agent.observability",
            f"assistant_agent.observability.{name}",
        }

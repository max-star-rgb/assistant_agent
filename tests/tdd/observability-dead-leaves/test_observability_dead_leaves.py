from importlib import import_module

import pytest


def test_only_observability_modules_with_live_consumers_remain() -> None:
    import_module("assistant_agent.observability.langsmith_native")
    import_module("assistant_agent.observability.trace_store")

    retired = (
        "agent_service_delivery",
        "agent_service_latency",
        "trace_conversation",
        "trace_ledger",
        "trace_metrics",
        "trace_persistence",
        "visual_trace_content",
    )
    for name in retired:
        with pytest.raises(ModuleNotFoundError) as exc_info:
            import_module(f"assistant_agent.observability.{name}")
        assert exc_info.value.name == f"assistant_agent.observability.{name}"

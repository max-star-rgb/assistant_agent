from __future__ import annotations

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime import runtime as runtime_module
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.ids import VISUAL_MEMORY_SEARCH_TOOL_NAME


class _Counter:
    tokenizer_id = "runtime-timeline-counter"

    def count_text(self, value: str) -> int:
        return len(value)


class _Compactor:
    def compact(self, **_kwargs):
        raise AssertionError("runtime wiring test must not execute compaction")


def test_runtime_attaches_visual_timeline_service_to_default_tool(
    monkeypatch,
) -> None:
    counter = _Counter()
    compactor = _Compactor()
    monkeypatch.setattr(
        runtime_module,
        "create_visual_context_token_counter",
        lambda _config: counter,
    )
    monkeypatch.setattr(
        runtime_module,
        "create_visual_timeline_compactor",
        lambda _config, _adapter, *, token_counter: (
            compactor if token_counter is counter else None
        ),
    )
    runtime = AgentGraphRuntime(config=ProviderConfig())
    try:
        tool = runtime.registry.get(VISUAL_MEMORY_SEARCH_TOOL_NAME)

        assert runtime.visual_timeline_compactor is compactor
        assert tool.timeline_context_service is runtime.visual_timeline_context_service
        assert tool.timeline_context_service.token_counter is counter
        assert (
            tool.timeline_context_service.window_policy
            is runtime.visual_context_window_policy
        )
    finally:
        runtime.close()

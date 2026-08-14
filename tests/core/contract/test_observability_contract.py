from __future__ import annotations

import asyncio
import inspect

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage
import pytest

from assistant_agent.agent_server import services
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.providers import MockAssistantChatModel


class _Events(BaseCallbackHandler):
    def __init__(self) -> None:
        self.names = []

    def on_chain_start(self, *args, **kwargs) -> None:
        self.names.append("chain")

    def on_chat_model_start(self, *args, **kwargs) -> None:
        self.names.append("chat_model")


@pytest.mark.core_invariant("OBS-001")
def test_native_callbacks_observe_actual_compiled_graph() -> None:
    events = _Events()
    graph = build_fast_agent(MockAssistantChatModel(), [])

    asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="trace-sentinel")]},
            context=AssistantRunContext(
                user_id="user-sentinel",
                tenant_id="tenant-sentinel",
            ),
            config={"callbacks": [events]},
        )
    )

    assert "chain" in events.names
    assert "chat_model" in events.names


@pytest.mark.core_invariant("OBS-001")
def test_production_composition_does_not_rebuild_shadow_trace_tree() -> None:
    source = inspect.getsource(services)

    assert "ProductEventProjector" not in source
    assert "InMemoryTraceStore" not in source
    assert "canonical" not in source

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from deepagents.backends import FilesystemBackend, LocalShellBackend
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
import pytest

from assistant_agent.agent_server import services
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.assistant_agent import build_assistant_agent
from assistant_agent.native_agent.providers import MockAssistantChatModel


class _Events(BaseCallbackHandler):
    def __init__(self) -> None:
        self.names = []

    def on_chain_start(self, *args, **kwargs) -> None:
        self.names.append("chain")

    def on_chat_model_start(self, *args, **kwargs) -> None:
        self.names.append("chat_model")


@pytest.mark.core_invariant("OBS-001")
def test_native_callbacks_observe_actual_compiled_graph(tmp_path: Path) -> None:
    events = _Events()
    graph = build_assistant_agent(
        MockAssistantChatModel(),
        [],
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        worker_graph=RunnableLambda(lambda state: state),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=(),
    )

    asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="trace-sentinel")]},
            context=AssistantRunContext(),
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

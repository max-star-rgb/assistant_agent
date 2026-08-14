"""Run-local resources for the LangGraph-native production composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.store.base import BaseStore
from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import load_mcp_server_configs_from_env
from assistant_agent.media.visual_perception import get_visual_perception_module
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.memory import MemoryBackend, create_memory_backend
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.root_graph import build_assistant_root_graph
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_mcp_tools,
    create_native_tools,
)


@dataclass
class AgentServerExecutionOwner:
    """Own only SDK resources used by one native graph composition."""

    model: BaseChatModel
    tools: list[BaseTool]
    memory_backend: MemoryBackend
    graph: Any
    _close_targets: tuple[Any, ...] = ()

    @classmethod
    async def compose(
        cls,
        *,
        store: BaseStore | None,
    ) -> "AgentServerExecutionOwner":
        """Build configured clients without blocking the Agent Server loop."""

        config = ProviderConfig.from_env()
        model, local_tools, memory_backend = await asyncio.to_thread(
            _compose_sync,
            config,
            store,
        )
        mcp_tools = await create_mcp_tools(load_mcp_server_configs_from_env())
        tools = [*local_tools, *mcp_tools]
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("native and MCP tool names must be unique")
        fast_agent = build_fast_agent(
            model,
            tools,
            model_call_limit=config.max_tool_iterations,
            tool_call_limit=config.max_tool_iterations,
            context_window_tokens=config.context_input_token_limit,
        )
        planning_graph = build_planning_graph(model, fast_agent)
        graph = build_assistant_root_graph(
            memory_backend=memory_backend,
            fast_agent=fast_agent,
            planning_graph=planning_graph,
        )
        return cls(
            model=model,
            tools=tools,
            memory_backend=memory_backend,
            graph=graph,
            _close_targets=(memory_backend, model, *tools),
        )

    async def aclose(self) -> None:
        seen: set[int] = set()
        for target in self._close_targets:
            if id(target) in seen:
                continue
            seen.add(id(target))
            await _close_if_supported(target)


def _compose_sync(
    config: ProviderConfig,
    store: BaseStore | None,
) -> tuple[BaseChatModel, list[BaseTool], MemoryBackend]:
    model = create_chat_model(config)
    visual_perception = get_visual_perception_module(config)
    visual_resources = visual_perception.tool_resources()
    tools = create_native_tools(
        config,
        resources=NativeToolResources(
            video_context_store=visual_resources.video_context_store,
            vision_client=visual_resources.vision_client,
            realtime_video_memory_store=(visual_resources.realtime_video_memory_store),
            visual_semantic_store_pool=(visual_resources.visual_semantic_store_pool),
            visual_memory_text_index=visual_resources.visual_memory_text_index,
        ),
    )
    memory_backend = create_memory_backend(
        config,
        langmem_store=store,
    )
    return model, tools, memory_backend


async def _close_if_supported(value: Any) -> None:
    closer = getattr(value, "aclose", None) or getattr(value, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


__all__ = ["AgentServerExecutionOwner"]

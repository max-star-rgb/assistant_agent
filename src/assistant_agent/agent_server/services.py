"""Run-local resources for the LangGraph-native production composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import inspect
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.store.base import BaseStore

from assistant_agent.agent_server.async_delegation import (
    ASYNC_TASK_INTERRUPT_TOOL_NAMES,
    async_task_tool_profile,
    build_async_subagent_middleware,
)
from assistant_agent.config import AppConfig, ProviderConfig, load_app_config
from assistant_agent.context.token_counter import create_context_token_counter
from assistant_agent.mcp.config import load_mcp_server_configs_from_env
from assistant_agent.mcp.stateful_sessions import ThreadMcpSessionPool
from assistant_agent.media.visual_perception import get_visual_perception_module
from assistant_agent.native_agent.assistant_agent import (
    build_assistant_agent,
    build_general_purpose_worker,
)
from assistant_agent.native_agent.memory import MemoryBackend, create_memory_backend
from assistant_agent.native_agent.memory_graph import build_memory_extraction_graph
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.tool_profiles import project_tool_profiles
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_native_tool_inventory,
    general_purpose_tool_names,
    interrupt_tool_names,
)
from assistant_agent.skills.native import create_project_skills_backend
from assistant_agent.runtime.local_backend import (
    create_conversation_history_backend,
    create_local_backend,
)
from assistant_agent.runtime.thread_resources import (
    ThreadResourceConfig,
    ThreadResourceManager,
)


async def reap_thread_resources(
    pool: ThreadMcpSessionPool,
    manager: ThreadResourceManager,
) -> None:
    """Close thread MCP sessions before deleting expired thread directories."""

    await pool.aclose_idle(manager.config.ttl_seconds)
    thread_refs = await asyncio.to_thread(manager.expired_thread_refs)
    for thread_ref in thread_refs:
        await asyncio.to_thread(manager.remove_expired, thread_ref)


async def _run_thread_resource_reaper(
    pool: ThreadMcpSessionPool,
    manager: ThreadResourceManager,
) -> None:
    while True:
        await asyncio.sleep(60)
        await reap_thread_resources(pool, manager)


@dataclass
class AgentServerExecutionOwner:
    """Own only SDK resources used by one native graph composition."""

    model: BaseChatModel
    tools: list[BaseTool]
    thread_resource_manager: ThreadResourceManager
    mcp_session_pool: ThreadMcpSessionPool
    memory_backend: MemoryBackend
    graph: Any
    worker_graph: Any
    memory_graph: Any
    thread_resource_reaper_task: asyncio.Task[None] | None = None

    @classmethod
    async def compose(
        cls,
        *,
        store: BaseStore | None,
    ) -> "AgentServerExecutionOwner":
        """Build configured clients without blocking the Agent Server loop."""

        # Temporary migration bridge: Task 5 removes the legacy config once
        # Vision and Tool composition accept their projected sections.
        app_config = load_app_config()
        config = ProviderConfig.from_env()
        context_token_counter = await asyncio.to_thread(
            create_context_token_counter,
            app_config.chat,
            provider_mode=app_config.provider_mode,
        )
        (
            model,
            tool_resources,
            memory_backend,
            project_root,
            thread_resource_config,
        ) = await asyncio.to_thread(
            _compose_sync,
            config,
            app_config,
            store,
        )
        thread_resource_manager = ThreadResourceManager(thread_resource_config)
        tool_resources = replace(
            tool_resources,
            thread_resource_manager=thread_resource_manager,
        )
        mcp_server_configs = load_mcp_server_configs_from_env()
        mcp_session_pool = ThreadMcpSessionPool(
            mcp_server_configs,
        )
        await reap_thread_resources(mcp_session_pool, thread_resource_manager)
        tools = await create_native_tool_inventory(
            config,
            resources=tool_resources,
            mcp_server_configs=mcp_server_configs,
            mcp_session_pool=mcp_session_pool,
        )
        general_purpose_names = general_purpose_tool_names(
            tools,
            mcp_server_configs,
        )
        interrupt_names = interrupt_tool_names(tools, mcp_server_configs)
        browser_profile = next(
            profile
            for profile in project_tool_profiles()
            if profile.profile_id == "browser"
        )
        browser_tool_names = set(browser_profile.tool_names)
        browser_tools = [tool for tool in tools if tool.name in browser_tool_names]
        general_purpose_tools = [
            tool
            for tool in tools
            if tool.name in general_purpose_names
            and tool.name not in browser_tool_names
        ]
        skills_backend = await asyncio.to_thread(
            create_project_skills_backend,
            project_root,
        )
        business_tool_profiles = project_tool_profiles()
        async_tool_profile = async_task_tool_profile()
        context_options = {
            "context_window_tokens": config.context_input_token_limit,
            "compaction_trigger_ratio": config.context_compaction_trigger_ratio,
            "compaction_target_ratio": config.context_compaction_target_ratio,
            "token_counter": (
                context_token_counter.count_messages
                if context_token_counter is not None
                else None
            ),
        }
        native_search_enabled = (
            config.provider_mode == "real"
            and config.resolved_chat_provider().provider == "qwen"
            and config.qwen_chat_api_protocol == "dashscope"
            and config.qwen_chat_enable_search
        )
        writable_backend = create_local_backend()
        conversation_history_backend = create_conversation_history_backend()
        worker_graph = build_general_purpose_worker(
            model,
            tools,
            backend=writable_backend,
            summarization_backend=conversation_history_backend,
            skills_backend=skills_backend,
            **context_options,
            tool_profiles=business_tool_profiles,
            general_purpose_tool_names={tool.name for tool in general_purpose_tools},
            interrupt_tool_names=interrupt_names,
            visual_history_probe=tool_resources.visual_history_probe,
            live_view_resolver=tool_resources.live_view_resolver,
            current_location=config.current_location,
            native_search_enabled=native_search_enabled,
        )
        async_middleware = build_async_subagent_middleware()
        assistant_agent = build_assistant_agent(
            model,
            tools,
            backend=writable_backend,
            summarization_backend=conversation_history_backend,
            worker_graph=worker_graph,
            skills_backend=skills_backend,
            **context_options,
            tool_profiles=(*business_tool_profiles, async_tool_profile),
            general_purpose_tool_names={tool.name for tool in general_purpose_tools},
            interrupt_tool_names={
                *interrupt_names,
                *ASYNC_TASK_INTERRUPT_TOOL_NAMES,
            },
            browser_tools=browser_tools,
            additional_middleware=(async_middleware,),
            visual_history_probe=tool_resources.visual_history_probe,
            live_view_resolver=tool_resources.live_view_resolver,
            current_location=config.current_location,
            native_search_enabled=native_search_enabled,
            memory_backend=memory_backend,
            memory_extraction_delay_seconds=config.memory_extraction_delay_seconds,
        )
        memory_graph = build_memory_extraction_graph(backend=memory_backend)
        owner = cls(
            model=model,
            tools=tools,
            thread_resource_manager=thread_resource_manager,
            mcp_session_pool=mcp_session_pool,
            memory_backend=memory_backend,
            graph=assistant_agent,
            worker_graph=worker_graph,
            memory_graph=memory_graph,
        )
        owner.thread_resource_reaper_task = asyncio.create_task(
            _run_thread_resource_reaper(mcp_session_pool, thread_resource_manager),
            name="thread-resource-reaper",
        )
        return owner

    async def aclose(self) -> None:
        if self.thread_resource_reaper_task is not None:
            self.thread_resource_reaper_task.cancel()
            await asyncio.gather(
                self.thread_resource_reaper_task,
                return_exceptions=True,
            )
            self.thread_resource_reaper_task = None
        seen: set[int] = set()
        for target in (
            self.mcp_session_pool,
            self.memory_backend,
            self.thread_resource_manager,
            self.model,
            *self.tools,
        ):
            if id(target) in seen:
                continue
            seen.add(id(target))
            await _close_if_supported(target)


def _compose_sync(
    config: ProviderConfig,
    app_config: AppConfig,
    store: BaseStore | None,
) -> tuple[
    BaseChatModel,
    NativeToolResources,
    MemoryBackend,
    Path,
    ThreadResourceConfig,
]:
    model = create_chat_model(
        app_config.chat,
        provider_mode=app_config.provider_mode,
    )
    visual_perception = get_visual_perception_module(config)
    visual_resources = visual_perception.tool_resources()
    tool_resources = NativeToolResources(
        video_context_store=visual_resources.video_context_store,
        vision_client=visual_resources.vision_client,
        realtime_video_memory_store=(visual_resources.realtime_video_memory_store),
        visual_semantic_store_pool=(visual_resources.visual_semantic_store_pool),
        visual_memory_text_index=visual_resources.visual_memory_text_index,
        visual_history_probe=visual_resources.visual_history_probe,
        embedding_coordinator_store=(visual_resources.embedding_coordinator_store),
        visual_reminder_registry=visual_resources.visual_reminder_registry,
        live_view_resolver=visual_perception.resolve_frozen_live_view,
    )
    memory_backend = create_memory_backend(
        app_config.memory,
        provider_mode=app_config.provider_mode,
        chat_config=app_config.chat,
        media_config=app_config.media,
        langmem_store=store,
    )
    project_root = Path(__file__).resolve().parents[3]
    thread_resource_config = ThreadResourceConfig(
        root=Path.home() / "assistant_agent" / "threads",
    )
    return (
        model,
        tool_resources,
        memory_backend,
        project_root,
        thread_resource_config,
    )


async def _close_if_supported(value: Any) -> None:
    closer = getattr(value, "aclose", None) or getattr(value, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


__all__ = ["AgentServerExecutionOwner"]

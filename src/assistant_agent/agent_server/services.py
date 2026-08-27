"""Run-local resources for the LangGraph-native production composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.store.base import BaseStore

from assistant_agent.coding.config import CodingConfig, CodingRepositoryConfig
from assistant_agent.coding.backend import (
    CodingWorkspaceBackend,
    ReadOnlyCodingWorkspaceBackend,
)
from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.agent_server.async_delegation import (
    async_task_tool_profile,
    build_async_subagent_middleware,
)
from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import load_mcp_server_configs_from_env
from assistant_agent.media.visual_perception import get_visual_perception_module
from assistant_agent.native_agent.assistant_agent import (
    build_assistant_agent,
    build_read_only_worker,
)
from assistant_agent.native_agent.memory import MemoryBackend, create_memory_backend
from assistant_agent.native_agent.memory_graph import build_memory_extraction_graph
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.root_graph import build_assistant_root_graph
from assistant_agent.native_agent.tool_profiles import project_tool_profiles
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_native_tool_inventory,
)
from assistant_agent.skills.native import create_project_skills_backend


@dataclass
class AgentServerExecutionOwner:
    """Own only SDK resources used by one native graph composition."""

    model: BaseChatModel
    tools: list[BaseTool]
    coding_workspace_service: CodingWorkspaceService
    memory_backend: MemoryBackend
    graph: Any
    worker_graph: Any
    memory_graph: Any

    @classmethod
    async def compose(
        cls,
        *,
        store: BaseStore | None,
    ) -> "AgentServerExecutionOwner":
        """Build configured clients without blocking the Agent Server loop."""

        config = ProviderConfig.from_env()
        (
            model,
            tool_resources,
            memory_backend,
            project_root,
            coding_repo_id,
            coding_config,
        ) = await asyncio.to_thread(
            _compose_sync,
            config,
            store,
        )
        tools = await create_native_tool_inventory(
            config,
            resources=tool_resources,
            mcp_server_configs=load_mcp_server_configs_from_env(),
        )
        coding_workspace_service = CodingWorkspaceService(coding_config)
        skills_backend = await asyncio.to_thread(
            create_project_skills_backend,
            project_root,
        )
        business_tool_profiles = project_tool_profiles()
        async_tool_profile = async_task_tool_profile()
        read_only_backend = ReadOnlyCodingWorkspaceBackend(
            coding_workspace_service,
            coding_repo_id,
        )
        worker_graph = build_read_only_worker(
            model,
            tools,
            backend=read_only_backend,
            skills_backend=skills_backend,
            tool_profiles=business_tool_profiles,
            visual_history_probe=tool_resources.visual_history_probe,
            live_view_resolver=tool_resources.live_view_resolver,
            current_location=config.current_location,
        )
        async_middleware = build_async_subagent_middleware(
            coding_workspace_service,
            coding_repo_id,
        )
        writable_backend = CodingWorkspaceBackend(
            coding_workspace_service,
            coding_repo_id,
        )
        assistant_agent = build_assistant_agent(
            model,
            tools,
            backend=writable_backend,
            worker_graph=worker_graph,
            skills_backend=skills_backend,
            tool_profiles=(*business_tool_profiles, async_tool_profile),
            additional_middleware=(
                async_middleware,
            ),
            visual_history_probe=tool_resources.visual_history_probe,
            live_view_resolver=tool_resources.live_view_resolver,
            current_location=config.current_location,
        )
        graph = build_assistant_root_graph(
            memory_backend=memory_backend,
            assistant_agent=assistant_agent,
            extraction_delay_seconds=config.memory_extraction_delay_seconds,
        )
        memory_graph = build_memory_extraction_graph(backend=memory_backend)
        return cls(
            model=model,
            tools=tools,
            coding_workspace_service=coding_workspace_service,
            memory_backend=memory_backend,
            graph=graph,
            worker_graph=worker_graph,
            memory_graph=memory_graph,
        )

    async def aclose(self) -> None:
        seen: set[int] = set()
        for target in (
            self.memory_backend,
            self.coding_workspace_service,
            self.model,
            *self.tools,
        ):
            if id(target) in seen:
                continue
            seen.add(id(target))
            await _close_if_supported(target)


def _compose_sync(
    config: ProviderConfig,
    store: BaseStore | None,
) -> tuple[
    BaseChatModel,
    NativeToolResources,
    MemoryBackend,
    Path,
    str,
    CodingConfig,
]:
    model = create_chat_model(config)
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
        config,
        langmem_store=store,
    )
    project_root = Path(__file__).resolve().parents[3]
    coding_repo_id = "assistant-agent"
    coding_config = CodingConfig(
        enabled=True,
        repositories={
            coding_repo_id: CodingRepositoryConfig(
                repo_id=coding_repo_id,
                path=project_root,
                target_branch="assistant-local",
            )
        },
    )
    return (
        model,
        tool_resources,
        memory_backend,
        project_root,
        coding_repo_id,
        coding_config,
    )


async def _close_if_supported(value: Any) -> None:
    closer = getattr(value, "aclose", None) or getattr(value, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


__all__ = ["AgentServerExecutionOwner"]

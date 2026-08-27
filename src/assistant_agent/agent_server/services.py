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
from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.agent_server.attestation import (
    AgentServerExecutionAttestation,
    build_execution_attestation,
)
from assistant_agent.agent_server.async_delegation import (
    async_task_tool_profile,
    build_async_subagent_middleware,
)
from assistant_agent.config import ProviderConfig
from assistant_agent.context.token_counter import create_context_token_counter
from assistant_agent.mcp.config import load_mcp_server_configs_from_env
from assistant_agent.media.visual_perception import get_visual_perception_module
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.coding_agent import build_coding_agent
from assistant_agent.native_agent.memory import MemoryBackend, create_memory_backend
from assistant_agent.native_agent.memory_graph import build_memory_extraction_graph
from assistant_agent.native_agent.planning_agent import build_planning_agent
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.root_graph import build_assistant_root_graph
from assistant_agent.native_agent.tool_profiles import project_tool_profiles
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_native_tool_inventory,
)
from assistant_agent.skills.native import (
    PROJECT_FILESYSTEM_READ_TOOL_NAMES,
    create_project_filesystem_backend,
)


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
    execution_attestation: AgentServerExecutionAttestation

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
        filesystem_backend = await asyncio.to_thread(
            create_project_filesystem_backend,
            project_root,
        )
        tools = await create_native_tool_inventory(
            config,
            resources=tool_resources,
            mcp_server_configs=load_mcp_server_configs_from_env(),
        )
        context_token_counter = await asyncio.to_thread(
            create_context_token_counter,
            config,
        )
        shared_fast_options = {
            "context_window_tokens": config.context_input_token_limit,
            "compaction_trigger_ratio": config.context_compaction_trigger_ratio,
            "compaction_target_ratio": config.context_compaction_target_ratio,
            "token_counter": (
                context_token_counter.count_messages
                if context_token_counter is not None
                else None
            ),
            "visual_history_probe": tool_resources.visual_history_probe,
            "live_view_resolver": tool_resources.live_view_resolver,
            "filesystem_backend": filesystem_backend,
            "current_location": config.current_location,
        }
        business_tool_profiles = project_tool_profiles()
        filesystem_tool_profile = next(
            profile
            for profile in business_tool_profiles
            if profile.profile_id == "filesystem"
        )
        async_tool_profile = async_task_tool_profile()
        fast_agent = build_fast_agent(
            model,
            tools,
            tool_profiles=(*business_tool_profiles, async_tool_profile),
            additional_middleware=(build_async_subagent_middleware(),),
            **shared_fast_options,
        )
        worker_graph = build_fast_agent(
            model,
            [
                tool
                for tool in tools
                if (tool.metadata or {}).get("effect") == "read"
            ],
            name="AssistantBackgroundWorker",
            tool_profiles=business_tool_profiles,
            filesystem_tool_names=PROJECT_FILESYSTEM_READ_TOOL_NAMES,
            **shared_fast_options,
        )
        planning_agent = build_planning_agent(
            model,
            fast_agent,
            filesystem_backend=filesystem_backend,
            context_window_tokens=config.context_input_token_limit,
            compaction_trigger_ratio=config.context_compaction_trigger_ratio,
            compaction_target_ratio=config.context_compaction_target_ratio,
            token_counter=(
                context_token_counter.count_messages
                if context_token_counter is not None
                else None
            ),
            current_location=config.current_location,
            tool_profiles=(filesystem_tool_profile, async_tool_profile),
            additional_middleware=(build_async_subagent_middleware(),),
        )
        execution_attestation = build_execution_attestation(config, coding_config)
        coding_workspace_service = CodingWorkspaceService(coding_config)
        coding_agent = build_coding_agent(
            model,
            coding_workspace_service,
            repo_id=coding_repo_id,
        )
        graph = build_assistant_root_graph(
            memory_backend=memory_backend,
            fast_agent=fast_agent,
            planning_agent=planning_agent,
            coding_agent=coding_agent,
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
            execution_attestation=execution_attestation,
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

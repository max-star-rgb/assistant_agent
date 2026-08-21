"""Run-local resources for the LangGraph-native production composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.store.base import BaseStore

from assistant_agent.coding.config import CodingConfig
from assistant_agent.coding.artifact_egress import (
    ArtifactIngressBackend,
    DockerArtifactIngressBackend,
)
from assistant_agent.coding.integration import CodingIntegrationService
from assistant_agent.coding.dependency_egress import DockerDependencyFetcher
from assistant_agent.coding.credentials import EnvironmentCredentialBroker
from assistant_agent.coding.sandbox import (
    CodingSandboxBackend,
    DockerCodingSandboxBackend,
)
from assistant_agent.coding.tools import create_coding_tools
from assistant_agent.coding.validation import CodingValidationService
from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.config import ProviderConfig
from assistant_agent.context.token_counter import create_context_token_counter
from assistant_agent.mcp.config import load_mcp_server_configs_from_env
from assistant_agent.media.visual_perception import get_visual_perception_module
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.coding_graph import build_coding_graph
from assistant_agent.native_agent.memory import MemoryBackend, create_memory_backend
from assistant_agent.native_agent.memory_graph import build_memory_extraction_graph
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.root_graph import build_assistant_root_graph
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_native_tool_inventory,
)
from assistant_agent.skills.loading import (
    default_repo_root,
    load_repo_skill_descriptors,
)


@dataclass
class AgentServerExecutionOwner:
    """Own only SDK resources used by one native graph composition."""

    model: BaseChatModel
    tools: list[BaseTool]
    coding_tools: list[BaseTool]
    coding_workspace_service: CodingWorkspaceService
    coding_sandbox_backend: CodingSandboxBackend | None
    coding_dependency_fetcher: DockerDependencyFetcher | None
    coding_artifact_backend: ArtifactIngressBackend | None
    coding_validation_service: CodingValidationService
    coding_integration_service: CodingIntegrationService
    memory_backend: MemoryBackend
    graph: Any
    memory_graph: Any

    @classmethod
    async def compose(
        cls,
        *,
        store: BaseStore | None,
    ) -> "AgentServerExecutionOwner":
        """Build configured clients without blocking the Agent Server loop."""

        config = ProviderConfig.from_env()
        model, tool_resources, memory_backend = await asyncio.to_thread(
            _compose_sync,
            config,
            store,
        )
        skill_catalog = await asyncio.to_thread(
            load_repo_skill_descriptors,
            default_repo_root(),
        )
        tools = await create_native_tool_inventory(
            config,
            resources=tool_resources,
            mcp_server_configs=load_mcp_server_configs_from_env(),
            skill_catalog=skill_catalog,
        )
        context_token_counter = await asyncio.to_thread(
            create_context_token_counter,
            config,
        )
        fast_agent = build_fast_agent(
            model,
            tools,
            model_call_limit=config.max_tool_iterations,
            tool_call_limit=config.max_tool_iterations,
            context_window_tokens=config.context_input_token_limit,
            compaction_trigger_ratio=config.context_compaction_trigger_ratio,
            compaction_target_ratio=config.context_compaction_target_ratio,
            token_counter=(
                context_token_counter.count_messages
                if context_token_counter is not None
                else None
            ),
            visual_history_probe=tool_resources.visual_history_probe,
            live_view_resolver=tool_resources.live_view_resolver,
            skill_catalog=skill_catalog,
        )
        planning_graph = build_planning_graph(
            model,
            fast_agent,
            tools=tools,
            skill_catalog=skill_catalog,
        )
        coding_config = CodingConfig.from_env()
        coding_workspace_service = CodingWorkspaceService(coding_config)
        coding_sandbox_backend: CodingSandboxBackend | None = None
        coding_dependency_fetcher: DockerDependencyFetcher | None = None
        coding_artifact_backend: ArtifactIngressBackend | None = None
        if coding_config.enabled and any(
            repository.sandbox_enabled
            for repository in coding_config.repositories.values()
        ):
            coding_sandbox_backend = DockerCodingSandboxBackend()
        if coding_config.enabled and any(
            repository.dependency_profile is not None
            for repository in coding_config.repositories.values()
        ):
            coding_dependency_fetcher = DockerDependencyFetcher(
                credential_broker=(
                    EnvironmentCredentialBroker(coding_config.credential_profiles)
                    if coding_config.credential_profiles
                    else None
                )
            )
        if coding_config.enabled and any(
            repository.artifact_profile is not None
            for repository in coding_config.repositories.values()
        ):
            coding_artifact_backend = DockerArtifactIngressBackend(
                managed_bundle_root=(
                    coding_config.workspace_root / "validation" / "artifact-bundles"
                )
            )
        coding_validation_service = CodingValidationService(
            coding_workspace_service,
            sandbox_backend=coding_sandbox_backend,
            dependency_fetcher=coding_dependency_fetcher,
            artifact_backend=coding_artifact_backend,
        )
        coding_integration_service = CodingIntegrationService(coding_workspace_service)
        coding_tools = create_coding_tools(coding_workspace_service)
        coding_graph = build_coding_graph(
            model,
            coding_tools,
            coding_workspace_service,
            validation_service=coding_validation_service,
            integration_service=coding_integration_service,
            model_call_limit=config.max_tool_iterations,
            tool_call_limit=config.max_tool_iterations,
        )
        graph = build_assistant_root_graph(
            memory_backend=memory_backend,
            fast_agent=fast_agent,
            planning_graph=planning_graph,
            coding_graph=coding_graph,
            extraction_delay_seconds=config.memory_extraction_delay_seconds,
        )
        memory_graph = build_memory_extraction_graph(backend=memory_backend)
        return cls(
            model=model,
            tools=tools,
            coding_tools=coding_tools,
            coding_workspace_service=coding_workspace_service,
            coding_sandbox_backend=coding_sandbox_backend,
            coding_dependency_fetcher=coding_dependency_fetcher,
            coding_artifact_backend=coding_artifact_backend,
            coding_validation_service=coding_validation_service,
            coding_integration_service=coding_integration_service,
            memory_backend=memory_backend,
            graph=graph,
            memory_graph=memory_graph,
        )

    async def aclose(self) -> None:
        seen: set[int] = set()
        for target in (
            self.memory_backend,
            self.coding_workspace_service,
            self.coding_validation_service,
            self.coding_sandbox_backend,
            self.coding_dependency_fetcher,
            self.coding_artifact_backend,
            self.coding_integration_service,
            self.model,
            *self.tools,
            *self.coding_tools,
        ):
            if id(target) in seen:
                continue
            seen.add(id(target))
            await _close_if_supported(target)


def _compose_sync(
    config: ProviderConfig,
    store: BaseStore | None,
) -> tuple[BaseChatModel, NativeToolResources, MemoryBackend]:
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
    return model, tool_resources, memory_backend


async def _close_if_supported(value: Any) -> None:
    closer = getattr(value, "aclose", None) or getattr(value, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


__all__ = ["AgentServerExecutionOwner"]

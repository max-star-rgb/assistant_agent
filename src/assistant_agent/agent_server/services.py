"""Invocation-local composition for Agent Server graph execution.

This module owns Python service objects only. Agent Server remains the owner of
threads, runs, queueing, checkpoints, cancellation, and the injected Store.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph_sdk.auth.types import BaseUser

from assistant_agent.agent_server.context import AgentServerRunContext
from assistant_agent.config import ProviderConfig
from assistant_agent.context.service import ContextService
from assistant_agent.context.token_budget import ContextWindowPolicy
from assistant_agent.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.memory.factory import create_memory_node_bundle
from assistant_agent.memory.node_bundle import MemoryNodeBundle
from assistant_agent.multi_agent.models import DEFAULT_AGENT_ID
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.assistant_graph_state import (
    AssistantTurnState,
    assistant_turn_state_from_agent_state,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.chat_adapter import create_chat_adapter
from assistant_agent.runtime.graph_invocation_claims import (
    InMemoryGraphInvocationClaimStore,
)
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.run_phase import RunPhase
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.tool_operation_barrier import default_tool_operation_store
from assistant_agent.tools.plugins.registry_factory import create_default_registry


class AgentServerExecutionError(RuntimeError):
    """Trusted Agent Server execution context is missing or inconsistent."""


@dataclass
class AgentServerGraphWorker:
    """Hydrate one native run without owning execution lifecycle."""

    context: AgentServerRunContext
    config: ProviderConfig
    tool_executor: ToolExecutor
    chat_adapter: Any
    trace_store: InMemoryTraceStore
    invocation_claim_store: InMemoryGraphInvocationClaimStore

    def __post_init__(self) -> None:
        self._runtime_context: GraphRuntimeContext | None = None

    def bootstrap(
        self,
        value: Any,
        runtime: Runtime[AgentServerRunContext],
    ) -> AssistantTurnState:
        execution = runtime.execution_info
        if execution is None or not execution.thread_id or not execution.run_id:
            raise AgentServerExecutionError(
                "Agent Server execution_info must provide native thread_id and run_id."
            )
        if runtime.context != self.context:
            raise AgentServerExecutionError("Graph run context changed after composition.")
        request = UserRequest(
            user_id=self.context.user_id,
            session_id=execution.thread_id,
            text=value.text,
            assistant_mode=self.context.assistant_mode,
            metadata={
                "entry_profile": self.context.entry_profile,
                "media_capabilities": list(self.context.media_capabilities),
            },
        )
        state = AgentState.from_request(
            request,
            run_id=execution.run_id,
            trace_id=execution.run_id,
            agent_id=DEFAULT_AGENT_ID,
        )
        selection = select_prompt_tool_specs(
            request,
            self.tool_executor.registry.list_specs(),
            registry_generation=self.tool_executor.registry.generation,
        )
        state.run_tool_catalog = selection.run_tool_catalog
        context_service = ContextService(
            window_policy=ContextWindowPolicy(
                input_token_limit=self.config.context_input_token_limit,
                trigger_ratio=self.config.context_compaction_trigger_ratio,
                target_ratio=self.config.context_compaction_target_ratio,
                hard_ratio=self.config.context_compaction_hard_ratio,
                safety_margin_tokens=self.config.context_compaction_safety_margin_tokens,
                summary_max_tokens=self.config.context_summary_max_tokens,
            ),
            current_location=self.config.current_location,
            supports_developer_role=bool(
                getattr(
                    getattr(self.chat_adapter, "capabilities", None),
                    "supports_developer_role",
                    False,
                )
            ),
            chat_max_tokens=self.config.chat_max_tokens,
            deep_research_chat_max_tokens=self.config.deep_research_chat_max_tokens,
        )
        self._runtime_context = GraphRuntimeContext(
            tool_executor=self.tool_executor,
            chat_adapter=self.chat_adapter,
            context_service=context_service,
            trace_store=self.trace_store,
            agent_state=state,
            invocation_claim_store=self.invocation_claim_store,
            invocation_token=execution.run_id,
        )
        graph_state = assistant_turn_state_from_agent_state(state)
        graph_state["turn_origin_id"] = value.turn_origin_id
        graph_state["memory_origin_run_id"] = value.turn_origin_id
        graph_state["run_phase"] = RunPhase.ACT.value
        graph_state["max_assistant_iterations"] = self.config.max_tool_iterations
        graph_state["max_tool_calls_per_run"] = self.config.max_tool_iterations
        graph_state["max_action_tool_calls_per_run"] = self.config.max_tool_iterations
        graph_state["max_control_tool_calls_per_run"] = (
            self.config.max_control_tool_iterations
        )
        return validate_assistant_turn_state(graph_state)

    def resolve(
        self,
        _parent: dict[str, object],
        child: AssistantTurnState,
        runtime: Runtime[AgentServerRunContext],
    ) -> GraphRuntimeContext:
        context = self._runtime_context
        if context is None or context.agent_state is None:
            raise AgentServerExecutionError("Graph worker was not bootstrapped.")
        if child["request"]["user_id"] != self.context.user_id:
            raise AgentServerExecutionError("Checkpoint owner does not match run context.")
        if child["run"]["run_id"] != context.agent_state.run_id:
            raise AgentServerExecutionError("Checkpoint run does not match bootstrap identity.")
        return context


@dataclass
class AgentServerExecutionOwner:
    """Own resources opened for exactly one native Agent Server run."""

    worker: AgentServerGraphWorker
    memory_bundle: MemoryNodeBundle

    @classmethod
    async def open(
        cls,
        *,
        context: AgentServerRunContext,
        store: BaseStore,
        user: BaseUser,
    ) -> "AgentServerExecutionOwner":
        _authorize_context(user, context)
        return await asyncio.to_thread(
            cls._open_sync,
            context=context,
            store=store,
        )

    @classmethod
    def _open_sync(
        cls,
        *,
        context: AgentServerRunContext,
        store: BaseStore,
    ) -> "AgentServerExecutionOwner":
        """Build synchronous SDKs away from Agent Server's event loop."""

        config = ProviderConfig.from_env()
        registry = create_default_registry(config)
        chat_adapter = create_chat_adapter(config)
        worker = AgentServerGraphWorker(
            context=context,
            config=config,
            tool_executor=ToolExecutor(
                registry=registry,
                operation_store=default_tool_operation_store(),
            ),
            chat_adapter=chat_adapter,
            trace_store=InMemoryTraceStore(),
            invocation_claim_store=InMemoryGraphInvocationClaimStore(max_entries=32),
        )
        memory_bundle = create_memory_node_bundle(config, langmem_store=store)
        return cls(worker=worker, memory_bundle=memory_bundle)

    async def aclose(self) -> None:
        if self.memory_bundle.aclose is not None:
            await self.memory_bundle.aclose()
        await _close_if_supported(self.worker.chat_adapter)


def _authorize_context(user: BaseUser, context: AgentServerRunContext) -> None:
    identity = str(user.identity)
    permissions = set(getattr(user, "permissions", ()) or ())
    if identity == context.user_id:
        return
    if permissions.intersection({"assistant:invoke", "assistant:developer"}):
        return
    raise PermissionError("Authenticated principal cannot delegate this user context.")


async def _close_if_supported(value: Any) -> None:
    closer = getattr(value, "aclose", None) or getattr(value, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


__all__ = [
    "AgentServerExecutionError",
    "AgentServerExecutionOwner",
    "AgentServerGraphWorker",
]

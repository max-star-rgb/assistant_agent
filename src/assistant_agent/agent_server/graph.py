"""Agent Server-native root graph and deployment factory boundary."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field
from langgraph_sdk.runtime import ServerRuntime

from assistant_agent.agent_server.context import AgentServerRunContext
from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.runtime.assistant_graph_state import AssistantTurnState
from assistant_agent.runtime.assistant_loop_graph import (
    build_namespaced_assistant_loop_graph,
)
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext


class AgentServerGraphInput(BaseModel):
    """Product input accepted by the deployed graph, never checkpoint internals."""

    model_config = ConfigDict(extra="forbid", strict=True)

    turn_origin_id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=32_000)


class AgentServerGraphState(TypedDict, total=False):
    request_input: AgentServerGraphInput
    assistant_state: AssistantTurnState


class AgentServerGraphRequest(TypedDict):
    request_input: AgentServerGraphInput


class AgentServerGraphWorker(Protocol):
    """Run-owned bootstrap and service hydration used by the native graph."""

    def bootstrap(
        self,
        value: AgentServerGraphInput,
        runtime: Runtime[AgentServerRunContext],
    ) -> AssistantTurnState: ...

    def resolve(
        self,
        parent: dict[str, object],
        child: AssistantTurnState,
        runtime: Runtime[AgentServerRunContext],
    ) -> GraphRuntimeContext: ...


def build_agent_server_assistant_graph(
    *,
    worker: AgentServerGraphWorker,
    memory_bundle: Any | None = None,
    stop_after_bootstrap: bool = False,
) -> Any:
    """Build one native root graph without binding a saver or Store."""

    def bootstrap(
        state: AgentServerGraphState,
        runtime: Runtime[AgentServerRunContext],
    ) -> AgentServerGraphState:
        request_input = AgentServerGraphInput.model_validate(state["request_input"])
        return {"assistant_state": worker.bootstrap(request_input, runtime)}

    builder = StateGraph(
        AgentServerGraphState,
        input_schema=AgentServerGraphRequest,
        context_schema=AgentServerRunContext,
    )
    builder.add_node("bootstrap", bootstrap)
    builder.add_edge(START, "bootstrap")
    if stop_after_bootstrap:
        builder.add_edge("bootstrap", END)
    else:
        assistant_loop = build_namespaced_assistant_loop_graph(
            state_schema=AgentServerGraphState,
            context_schema=AgentServerRunContext,
            child_state_key="assistant_state",
            runtime_context_resolver=worker.resolve,
            profile="standard",
            graph_name="AssistantAgentServerLoop",
            memory_bundle=memory_bundle,
            bind_store=False,
        )
        builder.add_node("assistant_loop", assistant_loop)
        builder.add_edge("bootstrap", "assistant_loop")
        builder.add_edge("assistant_loop", END)
    return builder.compile(name="AssistantAgentServerGraph")


class _UnconfiguredWorker:
    def bootstrap(self, value: AgentServerGraphInput, runtime: Runtime[Any]) -> AssistantTurnState:
        raise RuntimeError("Agent Server graph factory has not configured worker services")

    def resolve(self, parent, child, runtime) -> GraphRuntimeContext:
        raise RuntimeError("Agent Server graph factory has not configured worker services")


@asynccontextmanager
async def assistant_graph(runtime: ServerRuntime[AgentServerRunContext]):
    """Return one fixed topology; open services only for native execution."""

    execution = runtime.execution_runtime
    if execution is None:
        yield build_agent_server_assistant_graph(worker=_UnconfiguredWorker())
        return
    owner = await AgentServerExecutionOwner.open(
        context=AgentServerRunContext.model_validate(execution.context),
        store=runtime.store,
        user=runtime.ensure_user(),
    )
    try:
        yield build_agent_server_assistant_graph(
            worker=owner.worker,
            memory_bundle=owner.memory_bundle,
        )
    finally:
        await owner.aclose()


__all__ = [
    "AgentServerGraphInput",
    "AgentServerGraphState",
    "AgentServerGraphWorker",
    "assistant_graph",
    "build_agent_server_assistant_graph",
]

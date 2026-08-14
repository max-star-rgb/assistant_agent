"""Agent Server lifespan factory for the native assistant parent graph."""

from __future__ import annotations

from contextlib import asynccontextmanager

from langgraph_sdk.runtime import ServerRuntime

from assistant_agent.agent_server.context import AgentServerRunContext
from assistant_agent.agent_server.services import AgentServerExecutionOwner


@asynccontextmanager
async def native_assistant_graph(runtime: ServerRuntime[AgentServerRunContext]):
    """Yield one unbound parent graph and close composition-owned resources."""

    execution = runtime.execution_runtime
    if execution is None:
        owner = await AgentServerExecutionOwner.compose(store=runtime.store)
    else:
        owner = await AgentServerExecutionOwner.open(
            context=AgentServerRunContext.model_validate(execution.context),
            store=runtime.store,
            user=runtime.ensure_user(),
        )
    try:
        yield owner.graph
    finally:
        await owner.aclose()


__all__ = ["native_assistant_graph"]

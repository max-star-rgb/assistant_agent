"""Agent Server lifespan factory for the native assistant parent graph."""

from __future__ import annotations

from contextlib import asynccontextmanager

from langgraph_sdk.runtime import ServerRuntime

from assistant_agent.agent_server.services import AgentServerExecutionOwner


@asynccontextmanager
async def native_assistant_graph(runtime: ServerRuntime):
    """Yield one unbound parent graph and close composition-owned resources."""

    owner = await AgentServerExecutionOwner.compose(store=runtime.store)
    try:
        yield owner.graph
    finally:
        await owner.aclose()


__all__ = ["native_assistant_graph"]

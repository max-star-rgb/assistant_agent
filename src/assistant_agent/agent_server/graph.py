"""Agent Server lifespan factory for the native assistant parent graph."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from langgraph_sdk.runtime import ServerRuntime

from assistant_agent.agent_server.services import AgentServerExecutionOwner


_process_owner: AgentServerExecutionOwner | None = None
_process_owner_lock = asyncio.Lock()


@asynccontextmanager
async def native_assistant_graph(runtime: ServerRuntime):
    """Yield the process-owned static graph for every Agent Server access."""

    owner = await _get_process_owner(store=runtime.store)
    yield owner.graph


@asynccontextmanager
async def native_memory_graph(runtime: ServerRuntime):
    """Yield the process-owned cold-path Memory graph."""

    owner = await _get_process_owner(store=runtime.store)
    yield owner.memory_graph


@asynccontextmanager
async def native_worker_graph(runtime: ServerRuntime):
    """Yield the independent read-only background worker graph."""

    owner = await _get_process_owner(store=runtime.store)
    yield owner.worker_graph


async def _get_process_owner(*, store) -> AgentServerExecutionOwner:
    global _process_owner
    if _process_owner is not None:
        return _process_owner
    async with _process_owner_lock:
        if _process_owner is None:
            _process_owner = await AgentServerExecutionOwner.compose(store=store)
        return _process_owner


async def close_native_assistant_graph() -> None:
    """Close the cached composition once during process shutdown."""

    global _process_owner
    async with _process_owner_lock:
        owner, _process_owner = _process_owner, None
    if owner is not None:
        await owner.aclose()


__all__ = [
    "close_native_assistant_graph",
    "native_assistant_graph",
    "native_memory_graph",
    "native_worker_graph",
]

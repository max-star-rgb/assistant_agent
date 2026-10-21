"""Business recall middleware and Realtime session-end extraction submission."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.runnables import RunnableLambda
from langgraph.runtime import Runtime
from langgraph_sdk import get_client, get_sync_client

from assistant_agent.agent_server.config import agent_server_url
from assistant_agent.media.runtime_media import without_uploaded_media_messages
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.memory import MemoryBackend, _langmem_namespace, recall_memory
from assistant_agent.native_agent.state import AssistantAgentState


MEMORY_ASSISTANT_ID = "assistant-memory-v1"
MEMORY_SOURCE_THREAD_KEY = "assistant_agent_source_thread_id"


class MemoryLifecycleMiddleware(
    AgentMiddleware[AssistantAgentState, AssistantRunContext]
):
    """Recall once before the business agent; extraction belongs to Realtime."""

    def __init__(
        self,
        backend: MemoryBackend,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._recall = RunnableLambda(
            self._recall_once,
            name="memory_recall",
        ).with_retry(stop_after_attempt=3, wait_exponential_jitter=False)

    def before_agent(
        self,
        state: AssistantAgentState,
        runtime: Runtime[AssistantRunContext],
    ) -> dict[str, Any]:
        return asyncio.run(self.abefore_agent(state, runtime))

    async def abefore_agent(
        self,
        state: AssistantAgentState,
        runtime: Runtime[AssistantRunContext],
    ) -> dict[str, Any]:
        if self._backend.backend_id == "disabled" or not runtime.context.enable_memory:
            return {"memory_context": ()}
        return await self._recall.ainvoke((state, runtime))

    async def _recall_once(
        self,
        payload: tuple[AssistantAgentState, Runtime[AssistantRunContext]],
    ) -> dict[str, Any]:
        state, runtime = payload
        return await recall_memory(state, runtime, backend=self._backend)


async def submit_memory_extraction(messages, *, identity: str, source_thread_id: str | None,
                                   backend=None):
    """Submit the official reflector immediately for a closed Realtime session."""
    if not source_thread_id:
        raise ValueError("memory extraction orchestration requires thread_id")
    reference = None
    if backend is not None and callable(getattr(backend, 'prepare_extraction', None)):
        reference = await backend.prepare_extraction(identity=identity, thread_id=source_thread_id, messages=messages)
    memory_thread_id = str(
        uuid5(NAMESPACE_URL, f"assistant-agent:memory:{source_thread_id}")
    )
    url = agent_server_url()
    headers = {"X-Assistant-User": identity}
    async with get_client(url=url, api_key=None, headers=headers) as client:
        memory_thread = await client.threads.create(
            thread_id=memory_thread_id,
            graph_id=MEMORY_ASSISTANT_ID,
            metadata={MEMORY_SOURCE_THREAD_KEY: source_thread_id},
            if_exists="do_nothing",
        )
        memory_thread_id = str(memory_thread["thread_id"])
        from langmem.reflection import RemoteReflectionExecutor

        def submit_reflection() -> None:
            # Sync SDK defaults to localhost:8123, unlike the async in-process client.
            with (
                get_sync_client(url=url, api_key=None, headers=headers) as sync_client,
                RemoteReflectionExecutor(
                    namespace=_langmem_namespace(identity),
                    reflector=MEMORY_ASSISTANT_ID,
                    client=client,
                    sync_client=sync_client,
                ) as reflection,
            ):
                # Wait for Server acceptance, not extraction completion.
                reflection.submit(
                    {"messages": [] if reference else without_uploaded_media_messages(list(messages)),
                     **({'memory_snapshot_reference': reference} if reference else {})},
                    config={"configurable": {"thread_id": memory_thread_id}},
                    thread_id=memory_thread_id,
                    after_seconds=0,
                ).result()

        # The official executor and sync client must close outside the event loop.
        await asyncio.to_thread(submit_reflection)
    return {}


__all__ = [
    "MEMORY_ASSISTANT_ID",
    "MemoryLifecycleMiddleware",
]

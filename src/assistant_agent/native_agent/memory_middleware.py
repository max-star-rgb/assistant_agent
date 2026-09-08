"""Run-scoped recall and delayed extraction through native agent middleware."""

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
from assistant_agent.native_agent.context import AssistantRunContext, authenticated_user_identity
from assistant_agent.native_agent.memory import MemoryBackend, _langmem_namespace, recall_memory
from assistant_agent.native_agent.state import AssistantAgentState


MEMORY_ASSISTANT_ID = "assistant-memory-v1"
DEFAULT_EXTRACTION_DELAY_SECONDS = 1800
MEMORY_SOURCE_THREAD_KEY = "assistant_agent_source_thread_id"


class MemoryLifecycleMiddleware(
    AgentMiddleware[AssistantAgentState, AssistantRunContext]
):
    """Recall once before the agent and debounce extraction after it."""

    def __init__(
        self,
        backend: MemoryBackend,
        *,
        extraction_delay_seconds: int = DEFAULT_EXTRACTION_DELAY_SECONDS,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._delay_seconds = extraction_delay_seconds
        self._recall = RunnableLambda(
            self._recall_once,
            name="memory_recall",
        ).with_retry(stop_after_attempt=3, wait_exponential_jitter=False)
        self._refresh = RunnableLambda(
            self._refresh_once,
            name="refresh_memory_extraction",
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

    async def aafter_agent(
        self,
        state: AssistantAgentState,
        runtime: Runtime[AssistantRunContext],
    ) -> dict[str, Any]:
        return await self._refresh.ainvoke((state, runtime))

    def after_agent(
        self,
        state: AssistantAgentState,
        runtime: Runtime[AssistantRunContext],
    ) -> dict[str, Any]:
        return asyncio.run(self.aafter_agent(state, runtime))

    async def _recall_once(
        self,
        payload: tuple[AssistantAgentState, Runtime[AssistantRunContext]],
    ) -> dict[str, Any]:
        state, runtime = payload
        return await recall_memory(state, runtime, backend=self._backend)

    async def _refresh_once(
        self,
        payload: tuple[AssistantAgentState, Runtime[AssistantRunContext]],
    ) -> dict[str, Any]:
        state, runtime = payload
        if self._backend.backend_id == "disabled" or not runtime.context.enable_memory:
            return {}
        source_thread_id = _execution_value(runtime, "thread_id")
        if not source_thread_id:
            raise ValueError("memory extraction orchestration requires thread_id")
        memory_thread_id = str(
            uuid5(NAMESPACE_URL, f"assistant-agent:memory:{source_thread_id}")
        )
        url = agent_server_url()
        headers = {"X-Assistant-User": authenticated_user_identity(runtime)}
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
                        namespace=_langmem_namespace(authenticated_user_identity(runtime)),
                        reflector=MEMORY_ASSISTANT_ID,
                        client=client,
                        sync_client=sync_client,
                    ) as reflection,
                ):
                    # Wait for Server acceptance, not the delayed extraction itself.
                    reflection.submit(
                        {"messages": without_uploaded_media_messages(list(state.get("messages", ())))},
                        config={"configurable": {"thread_id": memory_thread_id}},
                        thread_id=memory_thread_id,
                        after_seconds=self._delay_seconds,
                    ).result()

            # The official executor and sync client must close outside the event loop.
            await asyncio.to_thread(submit_reflection)
        return {}


def _execution_value(runtime: Runtime[Any], field: str) -> str | None:
    value = getattr(runtime.execution_info, field, None)
    return str(value) if value else None


__all__ = [
    "DEFAULT_EXTRACTION_DELAY_SECONDS",
    "MEMORY_ASSISTANT_ID",
    "MemoryLifecycleMiddleware",
]

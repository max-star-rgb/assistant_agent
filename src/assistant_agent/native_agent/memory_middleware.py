"""Run-scoped recall and delayed extraction through native agent middleware."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.runnables import RunnableLambda
from langgraph.runtime import Runtime
from langgraph_sdk import get_client

from assistant_agent.media.runtime_media import without_uploaded_media_messages
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.memory import MemoryBackend, recall_memory
from assistant_agent.native_agent.state import AssistantAgentState


MEMORY_ASSISTANT_ID = "assistant-memory-v1"
DEFAULT_EXTRACTION_DELAY_SECONDS = 1800
MEMORY_EXTRACTION_RUN_KIND = "memory_extraction"
MEMORY_SOURCE_THREAD_KEY = "assistant_agent_source_thread_id"
PENDING_RUN_PAGE_SIZE = 100


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
        client = get_client()
        memory_thread = await client.threads.create(
            thread_id=memory_thread_id,
            graph_id=MEMORY_ASSISTANT_ID,
            metadata={MEMORY_SOURCE_THREAD_KEY: source_thread_id},
            if_exists="do_nothing",
        )
        memory_thread_id = str(memory_thread["thread_id"])
        pending_runs: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await client.runs.list(
                memory_thread_id,
                status="pending",
                limit=PENDING_RUN_PAGE_SIZE,
                offset=offset,
            )
            pending_runs.extend(page)
            if len(page) < PENDING_RUN_PAGE_SIZE:
                break
            offset += len(page)
        for run in pending_runs:
            metadata = run.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if metadata.get("assistant_agent_run_kind") != MEMORY_EXTRACTION_RUN_KIND:
                continue
            await client.runs.cancel(
                memory_thread_id,
                str(run["run_id"]),
                wait=True,
                action="rollback",
            )
        await client.runs.create(
            thread_id=memory_thread_id,
            assistant_id=MEMORY_ASSISTANT_ID,
            input={
                "messages": without_uploaded_media_messages(
                    list(state.get("messages", ()))
                )
            },
            metadata={"assistant_agent_run_kind": MEMORY_EXTRACTION_RUN_KIND},
            after_seconds=self._delay_seconds,
            multitask_strategy="enqueue",
        )
        return {}


def _execution_value(runtime: Runtime[Any], field: str) -> str | None:
    value = getattr(runtime.execution_info, field, None)
    return str(value) if value else None


__all__ = [
    "DEFAULT_EXTRACTION_DELAY_SECONDS",
    "MEMORY_ASSISTANT_ID",
    "MEMORY_EXTRACTION_RUN_KIND",
    "MemoryLifecycleMiddleware",
]

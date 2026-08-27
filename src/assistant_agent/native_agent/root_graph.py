"""Production chat graph with run-scoped recall and delayed memory extraction."""

from __future__ import annotations

from functools import partial
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy
from langgraph_sdk import get_client

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.memory import (
    MemoryBackend,
    memory_recall_node,
)
from assistant_agent.native_agent.state import AssistantRootInput, AssistantRootState


MEMORY_ASSISTANT_ID = "assistant-memory-v1"
DEFAULT_EXTRACTION_DELAY_SECONDS = 1800
MEMORY_EXTRACTION_RUN_KIND = "memory_extraction"
MEMORY_SOURCE_THREAD_KEY = "assistant_agent_source_thread_id"
PENDING_RUN_PAGE_SIZE = 100


def build_assistant_root_graph(
    *,
    memory_backend: MemoryBackend,
    assistant_agent: Any,
    extraction_delay_seconds: int = DEFAULT_EXTRACTION_DELAY_SECONDS,
):
    """Compose recall, execution, and post-answer Memory debounce."""

    builder = StateGraph(
        AssistantRootState,
        input_schema=AssistantRootInput,
        context_schema=AssistantRunContext,
    )
    builder.add_node(
        "memory_recall",
        partial(memory_recall_node, backend=memory_backend),
        retry_policy=RetryPolicy(
            initial_interval=0,
            backoff_factor=0,
            max_attempts=3,
            jitter=False,
        ),
    )
    builder.add_node("assistant_agent", assistant_agent)
    builder.add_node(
        "refresh_memory_extraction",
        partial(
            refresh_memory_extraction_node,
            delay_seconds=extraction_delay_seconds,
            enabled=memory_backend.backend_id != "disabled",
        ),
        retry_policy=RetryPolicy(
            initial_interval=0,
            backoff_factor=0,
            max_attempts=3,
            jitter=False,
        ),
    )
    builder.add_edge(START, "memory_recall")
    builder.add_edge("memory_recall", "assistant_agent")
    builder.add_edge("assistant_agent", "refresh_memory_extraction")
    builder.add_edge("refresh_memory_extraction", END)
    return builder.compile(name="AssistantRootGraph")


async def refresh_memory_extraction_node(
    state: AssistantRootState,
    config: RunnableConfig,
    runtime: Runtime[AssistantRunContext],
    *,
    delay_seconds: int,
    enabled: bool,
    assistant_id: str = MEMORY_ASSISTANT_ID,
) -> dict[str, object]:
    """Replace the old delayed Memory run after producing the answer."""

    if not enabled or not runtime.context.enable_memory:
        return {}
    source_thread_id = _thread_id(config)
    memory_thread_id = str(
        uuid5(NAMESPACE_URL, f"assistant-agent:memory:{source_thread_id}")
    )
    client = get_client()
    memory_thread = await client.threads.create(
        thread_id=memory_thread_id,
        graph_id=assistant_id,
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
        assistant_id=assistant_id,
        input={"messages": list(state.get("messages", ()))},
        metadata={"assistant_agent_run_kind": MEMORY_EXTRACTION_RUN_KIND},
        after_seconds=delay_seconds,
        multitask_strategy="enqueue",
    )
    return {}


def _thread_id(config: RunnableConfig) -> str:
    thread_id = config.get("configurable", {}).get("thread_id")
    if not thread_id:
        raise ValueError("memory extraction orchestration requires thread_id")
    return str(thread_id)


__all__ = [
    "DEFAULT_EXTRACTION_DELAY_SECONDS",
    "MEMORY_ASSISTANT_ID",
    "MEMORY_EXTRACTION_RUN_KIND",
    "build_assistant_root_graph",
    "refresh_memory_extraction_node",
]

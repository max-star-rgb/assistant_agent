"""Production chat graph with run-scoped recall and delayed memory extraction."""

from __future__ import annotations

from functools import partial
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, RetryPolicy
from langgraph_sdk import get_client

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.memory import (
    MemoryBackend,
    memory_recall_degraded,
    memory_recall_node,
)
from assistant_agent.native_agent.runtime_facts import (
    capture_trusted_runtime_facts_node,
)
from assistant_agent.native_agent.state import AssistantRootInput, AssistantRootState


MEMORY_ASSISTANT_ID = "assistant-memory-v1"
DEFAULT_EXTRACTION_DELAY_SECONDS = 1800
MEMORY_EXTRACTION_RUN_KIND = "memory_extraction"
PENDING_RUN_PAGE_SIZE = 100


def build_assistant_root_graph(
    *,
    memory_backend: MemoryBackend,
    fast_agent: Any,
    planning_graph: Any,
    coding_graph: Any,
    extraction_delay_seconds: int = DEFAULT_EXTRACTION_DELAY_SECONDS,
):
    """Compose recall, execution, and post-answer Memory debounce."""

    builder = StateGraph(
        AssistantRootState,
        input_schema=AssistantRootInput,
        context_schema=AssistantRunContext,
    )
    builder.add_node(
        "capture_trusted_runtime_facts",
        capture_trusted_runtime_facts_node,
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
        error_handler=memory_recall_degraded,
    )
    builder.add_node("execution_router", execution_router_node)
    builder.add_node("fast_agent", fast_agent)
    builder.add_node("planning_graph", planning_graph)
    builder.add_node("coding_graph", coding_graph)
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
        error_handler=memory_extraction_refresh_degraded,
    )
    builder.add_edge(START, "capture_trusted_runtime_facts")
    builder.add_edge("capture_trusted_runtime_facts", "memory_recall")
    builder.add_edge("memory_recall", "execution_router")
    builder.add_conditional_edges(
        "execution_router",
        route_execution_mode,
        {
            "fast": "fast_agent",
            "planning": "planning_graph",
            "coding": "coding_graph",
        },
    )
    builder.add_edge("fast_agent", "refresh_memory_extraction")
    builder.add_edge("planning_graph", "refresh_memory_extraction")
    builder.add_edge("coding_graph", "refresh_memory_extraction")
    builder.add_edge("refresh_memory_extraction", END)
    return builder.compile(name="AssistantRootGraph")


def execution_router_node(
    state: AssistantRootState,
    runtime: Runtime[AssistantRunContext],
) -> dict[str, object]:
    """Normalize an optional assistant-scoped planning preset into graph state."""

    preset = runtime.context.assistant_execution_mode
    if preset is None:
        return {"execution_mode": state.get("execution_mode", "fast")}
    return {"execution_mode": "planning"}


def route_execution_mode(state: AssistantRootState) -> str:
    """Route only on the trusted structured execution mode."""

    mode = state.get("execution_mode", "fast")
    if mode not in {"fast", "planning", "coding"}:
        raise ValueError("unsupported execution mode")
    return mode


async def refresh_memory_extraction_node(
    state: AssistantRootState,
    config: RunnableConfig,
    *,
    delay_seconds: int,
    enabled: bool,
    assistant_id: str = MEMORY_ASSISTANT_ID,
) -> dict[str, object]:
    """Replace the old delayed Memory run after producing the answer."""

    if not enabled:
        return {}
    thread_id = _thread_id(config)
    client = get_client()
    pending_runs: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await client.runs.list(
            thread_id,
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
            thread_id,
            str(run["run_id"]),
            wait=True,
            action="rollback",
        )
    await client.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id,
        input={"messages": list(state.get("messages", ()))},
        metadata={"assistant_agent_run_kind": MEMORY_EXTRACTION_RUN_KIND},
        after_seconds=delay_seconds,
        multitask_strategy="enqueue",
    )
    return {}


def memory_extraction_refresh_degraded(
    _state: AssistantRootState,
    error: NodeError,
) -> Command[str]:
    """Keep post-answer Memory orchestration failure from invalidating an answer."""

    del error
    return Command(goto=END)


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
    "execution_router_node",
    "memory_extraction_refresh_degraded",
    "refresh_memory_extraction_node",
    "route_execution_mode",
]

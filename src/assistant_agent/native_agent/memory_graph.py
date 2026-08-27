"""Independent native graph for delayed long-term memory extraction."""

from __future__ import annotations

from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.memory import (
    MemoryBackend,
    memory_extract_node,
)
from assistant_agent.native_agent.state import (
    MemoryExtractionInput,
    MemoryExtractionState,
)


def build_memory_extraction_graph(*, backend: MemoryBackend):
    """Build the cold-path graph executed by an Agent Server delayed run."""

    builder = StateGraph(
        MemoryExtractionState,
        input_schema=MemoryExtractionInput,
        context_schema=AssistantRunContext,
    )
    builder.add_node(
        "memory_extract",
        partial(memory_extract_node, backend=backend),
        retry_policy=RetryPolicy(
            initial_interval=0,
            backoff_factor=0,
            max_attempts=3,
            jitter=False,
        ),
    )
    builder.add_edge(START, "memory_extract")
    builder.add_edge("memory_extract", END)
    return builder.compile(name="AssistantMemoryExtractionGraph")


__all__ = ["build_memory_extraction_graph"]

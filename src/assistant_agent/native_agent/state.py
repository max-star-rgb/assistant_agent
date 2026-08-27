"""Incremental channels for the native assistant parent graph."""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from deepagents import DeepAgentState
from langchain.agents import AgentState
from langchain_core.messages import AnyMessage
from langgraph.graph import MessagesState
from pydantic import BaseModel, ConfigDict, JsonValue

from assistant_agent.native_agent.models import ProviderSearchProfile

MemoryStatus = Literal["ready", "empty", "degraded"]


def merge_async_tasks(
    current: dict[str, dict[str, JsonValue]] | None,
    update: dict[str, dict[str, JsonValue]] | None,
) -> dict[str, dict[str, JsonValue]]:
    """Merge official async-subagent task records by stable task ID."""

    return {**(current or {}), **(update or {})}


AsyncTasks = Annotated[
    dict[str, dict[str, JsonValue]],
    merge_async_tasks,
]

class AssistantRootInput(BaseModel):
    """Strict public input for a new native assistant run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    messages: list[AnyMessage]


class MemoryExtractionInput(BaseModel):
    """Strict public input for an independent background Memory run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    messages: list[AnyMessage]


class AssistantRootState(MessagesState):
    """State shared only across parent-graph execution branches."""

    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    async_tasks: NotRequired[AsyncTasks]


class AssistantAgentState(DeepAgentState):
    """State used by the main assistant agent."""

    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    provider_search_profile: NotRequired[ProviderSearchProfile]
    async_tasks: NotRequired[AsyncTasks]


class AssistantReadOnlyWorkerState(AgentState):
    """Private state available to the isolated read-only worker."""

    memory_context: NotRequired[tuple[str, ...]]


class MemoryExtractionState(MessagesState):
    """Message-only state for the independent Memory extraction graph."""

    pass


__all__ = [
    "AsyncTasks",
    "AssistantAgentState",
    "AssistantReadOnlyWorkerState",
    "AssistantRootInput",
    "AssistantRootState",
    "MemoryExtractionInput",
    "MemoryExtractionState",
    "MemoryStatus",
    "merge_async_tasks",
]

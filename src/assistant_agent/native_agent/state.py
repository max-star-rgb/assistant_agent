"""Incremental channels for the native assistant parent graph."""

from __future__ import annotations

from typing import Annotated, NotRequired

from deepagents import DeepAgentState
from langchain.agents import AgentState
from langchain.agents.middleware.types import (
    OmitFromInput,
    OmitFromOutput,
    PrivateStateAttr,
)
from langchain_core.messages import AnyMessage
from langgraph.graph import MessagesState
from pydantic import BaseModel, ConfigDict, JsonValue


def merge_async_tasks(
    current: dict[str, dict[str, JsonValue]] | None,
    update: dict[str, dict[str, JsonValue]] | None,
) -> dict[str, dict[str, JsonValue]]:
    """Merge official async-subagent task records by stable task ID."""

    return {**(current or {}), **(update or {})}


AsyncTasks = Annotated[
    dict[str, dict[str, JsonValue]],
    merge_async_tasks,
    PrivateStateAttr,
]


class MemoryExtractionInput(BaseModel):
    """Strict public input for an independent background Memory run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    messages: list[AnyMessage]


class AssistantAgentState(DeepAgentState):
    """State used by the main assistant agent."""

    needs_verification: NotRequired[Annotated[bool, PrivateStateAttr]]
    verification_attempts: NotRequired[Annotated[int, PrivateStateAttr]]
    memory_context: NotRequired[
        Annotated[tuple[str, ...], OmitFromInput, OmitFromOutput]
    ]
    async_tasks: NotRequired[AsyncTasks]


class AssistantAsyncTaskState(AgentState):
    """Keep upstream async task state out of the public input schema."""

    async_tasks: NotRequired[AsyncTasks]


class AssistantWorkerState(AgentState):
    """Private state available to the isolated general-purpose worker."""

    memory_context: NotRequired[tuple[str, ...]]


class MemoryExtractionState(MessagesState):
    """Message-only state for the independent Memory extraction graph."""

    pass


__all__ = [
    "AsyncTasks",
    "AssistantAgentState",
    "AssistantAsyncTaskState",
    "AssistantWorkerState",
    "MemoryExtractionInput",
    "MemoryExtractionState",
    "merge_async_tasks",
]

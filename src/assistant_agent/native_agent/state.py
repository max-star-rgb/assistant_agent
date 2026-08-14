"""Incremental channels for the native assistant parent graph."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, NotRequired, Required

from langchain.agents import AgentState
from langchain_core.messages import AnyMessage
from pydantic import BaseModel, ConfigDict

from assistant_agent.native_agent.models import (
    NativePlanProposal,
    WorkerResult,
)

ExecutionMode = Literal["fast", "planning"]
MemoryStatus = Literal["ready", "empty", "degraded"]


class AssistantRootInput(BaseModel):
    """Strict public input for a new native assistant run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    messages: list[AnyMessage]
    execution_mode: ExecutionMode


class FastAgentState(AgentState):
    """State consumed by the reusable create_agent subgraph."""

    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    execution_mode: NotRequired[ExecutionMode]


class AssistantRootState(FastAgentState):
    """Minimal state shared across the parent graph's two branches."""

    execution_mode: Required[ExecutionMode]


class WorkerState(FastAgentState):
    """Narrow input/output state for one planning worker branch."""

    work_item_id: Required[str]
    objective: Required[str]
    dependency_results: NotRequired[tuple[WorkerResult, ...]]


class PlanningState(AgentState):
    """Planning-only channels kept out of the fast branch."""

    memory_context: Required[tuple[str, ...]]
    plan: NotRequired[NativePlanProposal]
    worker_results: NotRequired[Annotated[list[WorkerResult], operator.add]]


__all__ = [
    "AssistantRootInput",
    "AssistantRootState",
    "ExecutionMode",
    "FastAgentState",
    "MemoryStatus",
    "PlanningState",
    "WorkerState",
]

"""Incremental channels for the native assistant parent graph."""

from __future__ import annotations

import operator
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, NotRequired, Required

from langchain.agents import AgentState
from langchain_core.messages import AnyMessage
from langgraph.graph import MessagesState
from pydantic import BaseModel, ConfigDict, model_validator

from assistant_agent.coding.models import (
    CodingCommandEvidence,
    CodingPatchApplyResult,
    CodingPatchProposal,
    CodingPatchValidation,
    CodingTerminalResult,
)

from assistant_agent.native_agent.models import (
    NativePlanProposal,
    ProviderSearchProfile,
    WorkerResult,
)
from assistant_agent.native_agent.runtime_facts import TrustedRuntimeFacts

ExecutionMode = Literal["fast", "planning", "coding"]
MemoryStatus = Literal["ready", "empty", "degraded"]
AgentPhase = Literal["fast", "planner", "worker", "finalizer"]


class AssistantRootInput(BaseModel):
    """Strict public input for a new native assistant run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    messages: list[AnyMessage]
    execution_mode: ExecutionMode = "fast"
    coding_repo_id: str | None = None

    @model_validator(mode="after")
    def _coding_requires_repository(self) -> "AssistantRootInput":
        if self.execution_mode == "coding" and not (self.coding_repo_id or "").strip():
            raise ValueError("coding_repo_id is required in coding mode")
        return self


class MemoryExtractionInput(BaseModel):
    """Strict public input for an independent background Memory run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    messages: list[AnyMessage]


class AssistantRootState(MessagesState):
    """State shared only across parent-graph execution branches."""

    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    execution_mode: NotRequired[ExecutionMode]
    trusted_runtime_facts: NotRequired[TrustedRuntimeFacts]
    coding_repo_id: NotRequired[str]
    coding_result: NotRequired[CodingTerminalResult]


class FastAgentState(AgentState):
    """State consumed inside the reusable create_agent subgraph."""

    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    execution_mode: NotRequired[ExecutionMode]
    trusted_runtime_facts: NotRequired[TrustedRuntimeFacts]
    agent_phase: NotRequired[AgentPhase]
    worker_tool_allowlist: NotRequired[tuple[str, ...]]
    provider_search_profile: NotRequired[ProviderSearchProfile]
    active_skill_ids: NotRequired[Annotated[list[str], _merge_unique_strings]]
    skill_reference_grants: NotRequired[
        Annotated[dict[str, list[str]], _merge_reference_grants]
    ]


class MemoryExtractionState(MessagesState):
    """Message-only state for the independent Memory extraction graph."""

    pass


class WorkerState(FastAgentState):
    """Narrow input/output state for one planning worker branch."""

    work_item_id: Required[str]
    objective: Required[str]
    dependency_results: NotRequired[tuple[WorkerResult, ...]]


class PlanningState(AgentState):
    """Planning-only channels kept out of the fast branch."""

    memory_context: Required[tuple[str, ...]]
    memory_status: Required[MemoryStatus]
    trusted_runtime_facts: NotRequired[TrustedRuntimeFacts]
    plan: NotRequired[NativePlanProposal]
    worker_results: NotRequired[Annotated[list[WorkerResult], operator.add]]


class CodingState(AgentState):
    """Sequential coding channels kept out of fast and planning branches."""

    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    execution_mode: NotRequired[ExecutionMode]
    trusted_runtime_facts: NotRequired[TrustedRuntimeFacts]
    coding_repo_id: Required[str]
    workspace_ref: NotRequired[str]
    base_commit: NotRequired[str]
    draft_artifact: NotRequired[dict[str, object] | None]
    proposal: NotRequired[CodingPatchProposal | None]
    validation: NotRequired[CodingPatchValidation | None]
    approval_status: NotRequired[Literal["pending", "approved", "rejected"] | None]
    approval_origin: NotRequired[Literal["model", "formatter"]]
    applied_result: NotRequired[CodingPatchApplyResult | None]
    format_round: NotRequired[int]
    verification_evidence: NotRequired[
        Annotated[list[CodingCommandEvidence], operator.add]
    ]
    coding_result: NotRequired[CodingTerminalResult]


def _merge_unique_strings(
    current: Sequence[str] | None,
    update: Sequence[str] | None,
) -> list[str]:
    return list(dict.fromkeys([*(current or ()), *(update or ())]))


def _merge_reference_grants(
    current: Mapping[str, Sequence[str]] | None,
    update: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    merged = {
        skill_id: list(dict.fromkeys(reference_ids))
        for skill_id, reference_ids in (current or {}).items()
    }
    for skill_id, reference_ids in (update or {}).items():
        merged[skill_id] = list(
            dict.fromkeys([*merged.get(skill_id, ()), *reference_ids])
        )
    return merged


__all__ = [
    "AgentPhase",
    "AssistantRootInput",
    "AssistantRootState",
    "CodingState",
    "ExecutionMode",
    "FastAgentState",
    "MemoryExtractionInput",
    "MemoryExtractionState",
    "MemoryStatus",
    "PlanningState",
    "WorkerState",
]

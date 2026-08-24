"""Incremental channels for the native assistant parent graph."""

from __future__ import annotations

import operator
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, NotRequired, Required

from langchain.agents import AgentState
from langchain_core.messages import AnyMessage
from langgraph.graph import MessagesState
from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from assistant_agent.coding.analysis import AnalysisStatus, merge_analysis_results
from assistant_agent.coding.models import (
    CodingAnalysisResult,
    CodingAnalysisSnapshot,
    CodingAnalysisTask,
    CodingCommandEvidence,
    CodingCommitResult,
    CodingArtifactIngressPlan,
    CodingCredentialRequest,
    CodingDependencyPlan,
    CodingMergePreview,
    CodingMergeResult,
    CodingPatchApplyResult,
    CodingPatchProposal,
    CodingPatchValidation,
    CodingReviewInput,
    CodingReviewReport,
    CodingReviewerResult,
    CodingReviewTask,
    CodingRepairApprovalContext,
    CodingRepairAttempt,
    CodingRepairFailureEvidence,
    CodingTerminalResult,
)

from assistant_agent.native_agent.models import ProviderSearchProfile

ExecutionMode = Literal["fast", "planning", "coding"]
MemoryStatus = Literal["ready", "empty", "degraded"]
AnalysisSnapshotReleaseStatus = Literal["active", "released", "cleanup_pending"]


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
    trusted_runtime_facts: NotRequired[dict[str, object]]
    coding_repo_id: NotRequired[str]
    coding_result: NotRequired[CodingTerminalResult]


class FastAgentState(AgentState):
    """State consumed inside the reusable create_agent subgraph."""

    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    execution_mode: NotRequired[ExecutionMode]
    trusted_runtime_facts: NotRequired[dict[str, object]]
    provider_search_profile: NotRequired[ProviderSearchProfile]
    active_skill_ids: NotRequired[Annotated[list[str], _merge_unique_strings]]
    skill_reference_grants: NotRequired[
        Annotated[dict[str, list[str]], _merge_reference_grants]
    ]


class PlanningAgentState(AgentState):
    """State shared by parallel upstream `task` Tool invocations."""

    memory_context: NotRequired[
        Annotated[tuple[str, ...], _merge_identical_value]
    ]
    memory_status: NotRequired[Annotated[MemoryStatus, _merge_identical_value]]
    execution_mode: NotRequired[Annotated[ExecutionMode, _merge_identical_value]]
    trusted_runtime_facts: NotRequired[
        Annotated[dict[str, object], _merge_identical_value]
    ]
    provider_search_profile: NotRequired[
        Annotated[ProviderSearchProfile, _merge_identical_value]
    ]
    active_skill_ids: NotRequired[Annotated[list[str], _merge_unique_strings]]
    skill_reference_grants: NotRequired[
        Annotated[dict[str, list[str]], _merge_reference_grants]
    ]


class MemoryExtractionState(MessagesState):
    """Message-only state for the independent Memory extraction graph."""

    pass


class CodingAnalysisWorkerState(AgentState):
    """Narrow input state for one snapshot-bound coding analysis branch."""

    coding_repo_id: Required[str]
    workspace_ref: Required[str]
    base_commit: Required[str]
    analysis_snapshot: Required[CodingAnalysisSnapshot]
    analysis_task: Required[CodingAnalysisTask]
    provider_search_profile: Required[Literal["none"]]


class CodingState(AgentState):
    """Sequential coding channels kept out of fast and planning branches."""

    coding_cycle_generation: NotRequired[int]
    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    execution_mode: NotRequired[ExecutionMode]
    trusted_runtime_facts: NotRequired[dict[str, object]]
    coding_repo_id: Required[str]
    workspace_ref: NotRequired[str | None]
    base_commit: NotRequired[str | None]
    analysis_snapshot: NotRequired[CodingAnalysisSnapshot | None]
    analysis_tasks: NotRequired[tuple[CodingAnalysisTask, ...]]
    analysis_results: NotRequired[
        Annotated[list[CodingAnalysisResult], merge_analysis_results]
    ]
    analysis_status: NotRequired[AnalysisStatus | Literal["pending"] | None]
    analysis_snapshot_release_status: NotRequired[
        AnalysisSnapshotReleaseStatus | None
    ]
    analysis_context_consumed: NotRequired[bool]
    draft_artifact: NotRequired[dict[str, object] | None]
    proposal: NotRequired[CodingPatchProposal | None]
    validation: NotRequired[CodingPatchValidation | None]
    approval_status: NotRequired[Literal["pending", "approved", "rejected"] | None]
    approval_origin: NotRequired[Literal["model", "formatter", "repair"] | None]
    applied_result: NotRequired[CodingPatchApplyResult | None]
    approved_changed_paths: NotRequired[Annotated[list[str], _merge_unique_strings]]
    dependency_plan: NotRequired[CodingDependencyPlan | None]
    dependency_approval_status: NotRequired[
        Literal["pending", "approved", "rejected", "not_required"] | None
    ]
    credential_request: NotRequired[CodingCredentialRequest | None]
    credential_approval_status: NotRequired[
        Literal["pending", "approved", "rejected", "not_required"] | None
    ]
    artifact_ingress_plan: NotRequired[CodingArtifactIngressPlan | None]
    artifact_approval_status: NotRequired[
        Literal["pending", "approved", "rejected", "not_required"] | None
    ]
    format_round: NotRequired[int]
    verification_evidence: NotRequired[
        Annotated[list[CodingCommandEvidence], operator.add]
    ]
    validation_snapshot: NotRequired[CodingAnalysisSnapshot | None]
    validation_binding_digest: NotRequired[str | None]
    last_verification_status: NotRequired[Literal["passed", "failed"] | None]
    review_required: NotRequired[bool]
    review_generation: NotRequired[int | None]
    review_snapshot: NotRequired[CodingAnalysisSnapshot | None]
    review_snapshot_schema_version: NotRequired[
        Literal["legacy_v1", "immutable_manifest_v2"] | None
    ]
    review_snapshot_release_status: NotRequired[
        Literal["active", "released"] | None
    ]
    review_input: NotRequired[CodingReviewInput | None]
    review_tasks: NotRequired[tuple[CodingReviewTask, ...]]
    review_results: NotRequired[
        Annotated[list[CodingReviewerResult], operator.add]
    ]
    review_report: NotRequired[CodingReviewReport | None]
    review_status: NotRequired[Literal["clean", "findings", "unavailable"] | None]
    review_validation_digest: NotRequired[str | None]
    review_decision_context: NotRequired[dict[str, JsonValue] | None]
    review_decision: NotRequired[Literal["approved", "rejected"] | None]
    integration_required: NotRequired[bool]
    commit_result: NotRequired[CodingCommitResult | None]
    merge_preview: NotRequired[CodingMergePreview | None]
    merge_result: NotRequired[CodingMergeResult | None]
    repair_round: NotRequired[int]
    repair_status: NotRequired[
        Literal["pending", "active", "passed", "exhausted", "no_progress"] | None
    ]
    repair_failure_evidence: NotRequired[CodingRepairFailureEvidence | None]
    repair_history: NotRequired[Annotated[list[CodingRepairAttempt], operator.add]]
    repair_model_calls: NotRequired[int]
    repair_proposal_digests: NotRequired[Annotated[list[str], _merge_unique_strings]]
    repair_approval_context: NotRequired[CodingRepairApprovalContext | None]
    coding_result: NotRequired[CodingTerminalResult | None]


def _merge_unique_strings(
    current: Sequence[str] | None,
    update: Sequence[str] | None,
) -> list[str]:
    return list(dict.fromkeys([*(current or ()), *(update or ())]))


def _merge_identical_value(current, update):
    if current in (None, (), {}, ""):
        return update
    if update in (None, (), {}, "") or update == current:
        return current
    raise ValueError("parallel task results contain conflicting frozen state")


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
    "AnalysisSnapshotReleaseStatus",
    "AssistantRootInput",
    "AssistantRootState",
    "CodingAnalysisWorkerState",
    "CodingState",
    "ExecutionMode",
    "FastAgentState",
    "MemoryExtractionInput",
    "MemoryExtractionState",
    "MemoryStatus",
    "PlanningAgentState",
]

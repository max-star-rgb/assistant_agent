"""Incremental channels for the native assistant parent graph."""

from __future__ import annotations

import operator
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, NotRequired, Required, TypeVar

from langchain.agents import AgentState
from langchain_core.messages import AnyMessage
from langgraph.graph import MessagesState
from pydantic import BaseModel, ConfigDict, model_validator

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

from assistant_agent.native_agent.models import (
    BudgetUsage,
    NativePlanProposal,
    PlanningAuthorizationEnvelope,
    PlannerEvidence,
    PlannerOutcome,
    ProviderSearchProfile,
    RecoveryDecision,
    ReplacementClaim,
    WorkerOutcome,
    WorkerResult,
)
from assistant_agent.native_agent.planning_budget import WaveReservation
from pydantic import JsonValue

ExecutionMode = Literal["fast", "planning", "coding"]
MemoryStatus = Literal["ready", "empty", "degraded"]
AgentPhase = Literal["fast", "planner", "worker", "finalizer"]
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


def add_budget_usage(
    left: BudgetUsage | Mapping[str, object] | None,
    right: BudgetUsage | Mapping[str, object] | None,
) -> BudgetUsage:
    """Add phase usage counters without mutating either input model."""

    lhs = BudgetUsage.model_validate(left or {})
    rhs = BudgetUsage.model_validate(right or {})
    return BudgetUsage(
        model_calls=lhs.model_calls + rhs.model_calls,
        tool_calls=lhs.tool_calls + rhs.tool_calls,
        node_attempts=lhs.node_attempts + rhs.node_attempts,
        replans=lhs.replans + rhs.replans,
    )


class FastAgentState(AgentState):
    """State consumed inside the reusable create_agent subgraph."""

    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    execution_mode: NotRequired[ExecutionMode]
    trusted_runtime_facts: NotRequired[dict[str, object]]
    agent_phase: NotRequired[AgentPhase]
    phase_model_call_count: NotRequired[int]
    phase_tool_call_count: NotRequired[int]
    phase_budget_status: NotRequired[Literal["exhausted"]]
    phase_budget_usage: NotRequired[Annotated[BudgetUsage, add_budget_usage]]
    phase_budget_allowance: NotRequired[BudgetUsage]
    worker_tool_allowlist: NotRequired[tuple[str, ...]]
    provider_search_profile: NotRequired[ProviderSearchProfile]
    active_skill_ids: NotRequired[Annotated[list[str], _merge_unique_strings]]
    skill_reference_grants: NotRequired[
        Annotated[dict[str, list[str]], _merge_reference_grants]
    ]
    authorization_envelope: NotRequired[PlanningAuthorizationEnvelope]


class MemoryExtractionState(MessagesState):
    """Message-only state for the independent Memory extraction graph."""

    pass


class WorkerState(AgentState):
    """Narrow input/output state for one planning worker branch."""

    memory_context: Required[tuple[str, ...]]
    memory_status: Required[MemoryStatus]
    execution_mode: Required[ExecutionMode]
    trusted_runtime_facts: NotRequired[dict[str, object]]
    agent_phase: Required[AgentPhase]
    worker_tool_allowlist: Required[tuple[str, ...]]
    provider_search_profile: Required[ProviderSearchProfile]
    active_skill_ids: Required[list[str]]
    skill_reference_grants: Required[dict[str, list[str]]]
    execution_id: Required[str]
    plan_generation: Required[int]
    attempt: Required[int]
    tool_call_allowance: Required[int]
    budget_allowance: Required[BudgetUsage]
    work_item_id: Required[str]
    objective: Required[str]
    dependency_results: Required[tuple[WorkerResult, ...]]
    planner_evidence: Required[tuple[PlannerEvidence, ...]]


class CodingAnalysisWorkerState(AgentState):
    """Narrow input state for one snapshot-bound coding analysis branch."""

    coding_repo_id: Required[str]
    workspace_ref: Required[str]
    base_commit: Required[str]
    analysis_snapshot: Required[CodingAnalysisSnapshot]
    analysis_task: Required[CodingAnalysisTask]
    provider_search_profile: Required[Literal["none"]]


class PlanningState(AgentState):
    """Planning-only channels kept out of the fast branch."""

    memory_context: Required[tuple[str, ...]]
    memory_status: Required[MemoryStatus]
    trusted_runtime_facts: NotRequired[dict[str, object]]
    plan: NotRequired[NativePlanProposal]
    plan_candidate: NotRequired[NativePlanProposal | None]
    planner_active_skill_ids: NotRequired[Annotated[list[str], _merge_unique_strings]]
    planner_skill_reference_grants: NotRequired[
        Annotated[dict[str, list[str]], _merge_reference_grants]
    ]
    planner_evidence: NotRequired[
        Annotated[list[PlannerEvidence], _merge_planner_evidence]
    ]
    admission_error: NotRequired[str | None]
    revision_count: NotRequired[int]
    plan_generation: NotRequired[int]
    planner_attempt_count: NotRequired[int]
    planner_outcome: NotRequired[PlannerOutcome | None]
    authorization_envelope: NotRequired[
        Annotated[
            PlanningAuthorizationEnvelope | None,
            merge_planning_authorization_envelope,
        ]
    ]
    recovery_decision: NotRequired[RecoveryDecision | None]
    # True once the terminal phase's single node attempt has been settled.  A
    # controlled projection after a failed model finalizer reuses that attempt.
    terminal_attempt_charged: NotRequired[bool]
    recovery_context: NotRequired[dict[str, JsonValue] | None]
    recovery_history: NotRequired[list[RecoveryDecision]]
    # Sequential graph nodes publish an absolute total. Worker branches only emit
    # immutable outcomes; reconciliation is the sole worker accounting writer.
    budget_usage: NotRequired[BudgetUsage]
    wave_reservations: NotRequired[
        Annotated[dict[str, WaveReservation], merge_wave_reservations]
    ]
    reconciled_wave_reservation_ids: NotRequired[
        Annotated[list[str], _merge_unique_strings]
    ]
    historical_node_ids: NotRequired[Annotated[list[str], _merge_unique_strings]]
    replacement_claims: NotRequired[
        Annotated[dict[str, ReplacementClaim], merge_replacement_claims]
    ]
    superseded_work_item_ids: NotRequired[Annotated[list[str], _merge_unique_strings]]
    worker_outcomes: NotRequired[
        Annotated[dict[str, WorkerOutcome], merge_worker_outcomes]
    ]
    frozen_worker_results: NotRequired[
        Annotated[dict[str, WorkerResult], merge_frozen_worker_results]
    ]
    worker_attempts: NotRequired[dict[str, int]]
    # Compatibility projection only. Recovery and scheduling never read this channel.
    worker_results: NotRequired[list[WorkerResult]]


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


def _merge_planner_evidence(
    current: Sequence[PlannerEvidence] | None,
    update: Sequence[PlannerEvidence] | None,
) -> list[PlannerEvidence]:
    merged: list[PlannerEvidence] = []
    seen_ids: set[str] = set()
    for evidence in [*(current or ()), *(update or ())]:
        if evidence.evidence_id in seen_ids:
            continue
        seen_ids.add(evidence.evidence_id)
        merged.append(evidence)
    return merged


_ValueT = TypeVar("_ValueT")


def _merge_immutable_mapping(
    left: Mapping[str, _ValueT] | None,
    right: Mapping[str, _ValueT] | None,
    *,
    conflict_message: str,
) -> dict[str, _ValueT]:
    """Merge checkpoint/reducer updates without allowing last-write-wins."""

    merged = dict(left or {})
    for key, value in (right or {}).items():
        if key in merged:
            if merged[key] != value:
                raise ValueError(conflict_message)
            continue
        merged[key] = value
    return merged


def merge_worker_outcomes(
    left: Mapping[str, WorkerOutcome | Mapping[str, object]] | None,
    right: Mapping[str, WorkerOutcome | Mapping[str, object]] | None,
) -> dict[str, WorkerOutcome]:
    """Deterministically merge worker outcomes and reject conflicting replay."""

    return _merge_immutable_mapping(
        _validated_worker_outcome_mapping(left),
        _validated_worker_outcome_mapping(right),
        conflict_message="conflicting worker outcome",
    )


def merge_frozen_worker_results(
    left: Mapping[str, WorkerResult | Mapping[str, object]] | None,
    right: Mapping[str, WorkerResult | Mapping[str, object]] | None,
) -> dict[str, WorkerResult]:
    """Merge the monotonic frozen-result ledger."""

    return _merge_immutable_mapping(
        _validated_frozen_result_mapping(left),
        _validated_frozen_result_mapping(right),
        conflict_message="conflicting frozen worker result",
    )


def merge_planning_authorization_envelope(
    left: PlanningAuthorizationEnvelope | Mapping[str, object] | None,
    right: PlanningAuthorizationEnvelope | Mapping[str, object] | None,
) -> PlanningAuthorizationEnvelope | None:
    """Freeze the first admitted authorization scope and reject later conflicts."""

    lhs = PlanningAuthorizationEnvelope.model_validate(left) if left is not None else None
    rhs = PlanningAuthorizationEnvelope.model_validate(right) if right is not None else None
    if lhs is None:
        return rhs
    if rhs is None or rhs == lhs:
        return lhs
    raise ValueError("conflicting planning authorization envelope")


def merge_replacement_claims(
    left: Mapping[str, ReplacementClaim | Mapping[str, object]] | None,
    right: Mapping[str, ReplacementClaim | Mapping[str, object]] | None,
) -> dict[str, ReplacementClaim]:
    """Merge monotonic historical replacement claims without last-write-wins."""

    return _merge_immutable_mapping(
        _validated_replacement_claim_mapping(left),
        _validated_replacement_claim_mapping(right),
        conflict_message="conflicting historical replacement claim",
    )


def merge_wave_reservations(
    left: Mapping[str, WaveReservation | Mapping[str, object]] | None,
    right: Mapping[str, WaveReservation | Mapping[str, object]] | None,
) -> dict[str, WaveReservation]:
    """Merge the append-only reservation ledger and reject identity conflicts."""

    return _merge_immutable_mapping(
        _validated_wave_reservation_mapping(left),
        _validated_wave_reservation_mapping(right),
        conflict_message="conflicting wave reservation",
    )


def _validated_worker_outcome_mapping(
    values: Mapping[str, WorkerOutcome | Mapping[str, object]] | None,
) -> dict[str, WorkerOutcome]:
    validated: dict[str, WorkerOutcome] = {}
    for key, value in (values or {}).items():
        payload = value.model_dump() if isinstance(value, WorkerOutcome) else value
        outcome = WorkerOutcome.model_validate(payload)
        if key != outcome.execution_id:
            raise ValueError("worker outcome key does not match execution_id")
        validated[key] = outcome
    return validated


def _validated_frozen_result_mapping(
    values: Mapping[str, WorkerResult | Mapping[str, object]] | None,
) -> dict[str, WorkerResult]:
    validated: dict[str, WorkerResult] = {}
    for key, value in (values or {}).items():
        payload = value.model_dump() if isinstance(value, WorkerResult) else value
        result = WorkerResult.model_validate(payload)
        if key != result.work_item_id:
            raise ValueError("frozen worker result key does not match work_item_id")
        validated[key] = result
    return validated


def _validated_wave_reservation_mapping(
    values: Mapping[str, WaveReservation | Mapping[str, object]] | None,
) -> dict[str, WaveReservation]:
    validated: dict[str, WaveReservation] = {}
    for key, value in (values or {}).items():
        payload = value.model_dump() if isinstance(value, WaveReservation) else value
        reservation = WaveReservation.model_validate(payload)
        if key != reservation.execution_id:
            raise ValueError("wave reservation key does not match execution_id")
        validated[key] = reservation
    return validated


def _validated_replacement_claim_mapping(
    values: Mapping[str, ReplacementClaim | Mapping[str, object]] | None,
) -> dict[str, ReplacementClaim]:
    validated: dict[str, ReplacementClaim] = {}
    for key, value in (values or {}).items():
        payload = value.model_dump() if isinstance(value, ReplacementClaim) else value
        claim = ReplacementClaim.model_validate(payload)
        if key != claim.replaced_node_id:
            raise ValueError("replacement claim key does not match replaced_node_id")
        validated[key] = claim
    return validated


__all__ = [
    "AgentPhase",
    "add_budget_usage",
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
    "merge_frozen_worker_results",
    "merge_planning_authorization_envelope",
    "merge_replacement_claims",
    "merge_wave_reservations",
    "merge_worker_outcomes",
    "PlanningState",
    "WorkerState",
]

"""Strict checkpoint contracts for the native Durable Workflow graph.

Only bounded, JSON-safe execution facts cross this module. Process-owned
services, rich artifact bodies, provider responses and scheduler leases stay
outside LangGraph state.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal, Mapping, TypedDict, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from assistant_agent.runtime.assistant_graph_profiles import AssistantGraphProfileName
from assistant_agent.workflows.models import (
    WorkflowBudget,
    WorkflowPlanVersion,
    WorkflowRecord,
    WorkflowSubmission,
)


WORKFLOW_GRAPH_NAME = "DurableWorkflowGraph"
WORKFLOW_GRAPH_VERSION = "3"
WORKFLOW_STATE_SCHEMA_VERSION = 1
WorkflowPhase = Literal[
    "planning",
    "admitted",
    "executing",
    "verifying",
    "repairing",
    "waiting_input",
    "publishing",
    "completed",
    "failed",
    "cancelled",
]
WorkflowGraphStatus = Literal[
    "queued",
    "running",
    "waiting_input",
    "blocked",
    "recovering",
    "completed",
    "failed",
    "cancelled",
]

_NODE_ID_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_ARTIFACT_REF_PATTERN = re.compile(
    r"^(?:artifact|workflow-artifact|mock)://[A-Za-z0-9][A-Za-z0-9._~:/-]{0,1014}$"
)


class WorkflowGraphStateConflict(ValueError):
    """Raised when a derived view encounters non-deterministic branch output."""

    code = "workflow_result_conflict"


class WorkflowGraphStateCompatibilityError(ValueError):
    """Raised when a checkpoint is not consumable by graph version 3."""

    code = "workflow_graph_state_incompatible"


class _CheckpointModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _validate_artifact_ref(value: str) -> str:
    if not _ARTIFACT_REF_PATTERN.fullmatch(value):
        raise ValueError("artifact reference must be a bounded opaque URI")
    return value


class PersistedDeepResearchInputs(_CheckpointModel):
    schema_version: Literal["deep_research_inputs_v2"] = "deep_research_inputs_v2"
    research_questions: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_questions(self) -> "PersistedDeepResearchInputs":
        if any(
            not question.strip() or len(question) > 4_000
            for question in self.research_questions
        ):
            raise ValueError(
                "research_questions must contain bounded non-empty strings"
            )
        if len(self.research_questions) != len(set(self.research_questions)):
            raise ValueError("research_questions must be unique")
        return self


class PersistedWorkflowBudgetRequest(_CheckpointModel):
    model_calls: int | None = Field(default=None, ge=1, le=10_000)
    tool_calls: int | None = Field(default=None, ge=1, le=100_000)
    workflow_quanta: int | None = Field(default=None, ge=1, le=1_000_000)
    deadline_seconds: int | None = Field(default=None, ge=60, le=2_592_000)


class PersistedWorkflowSubmission(_CheckpointModel):
    workflow_type: Literal["deep_research"]
    objective: str = Field(min_length=1, max_length=10_000)
    deliverables: tuple[str, ...] = Field(min_length=1, max_length=32)
    constraints: tuple[str, ...] = Field(default=(), max_length=64)
    inputs: PersistedDeepResearchInputs
    requested_budget: PersistedWorkflowBudgetRequest
    durability_reasons: tuple[str, ...] = Field(min_length=1, max_length=16)
    seed_artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_refs_and_uniqueness(self) -> "PersistedWorkflowSubmission":
        if any(len(item) > 240 or not item.strip() for item in self.deliverables):
            raise ValueError("deliverables must be bounded non-empty strings")
        if any(len(item) > 4_000 or not item.strip() for item in self.constraints):
            raise ValueError("constraints must be bounded non-empty strings")
        for ref in self.seed_artifact_refs:
            _validate_artifact_ref(ref)
        return self


class PersistedWorkflowIdentity(_CheckpointModel):
    """Trusted owner and stable thread facts required to rebuild graph branches."""

    user_id: str = Field(min_length=1, max_length=512)
    session_id: str = Field(min_length=1, max_length=512)
    agent_id: str = Field(min_length=1, max_length=512)
    workflow_thread_id: str = Field(min_length=1, max_length=512)
    turn_origin_id: str = Field(min_length=1, max_length=512)


class PersistedWorkflowAcceptanceCriterion(_CheckpointModel):
    criterion_id: str = Field(pattern=_NODE_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=4_000)


class PersistedWorkflowArtifactContract(_CheckpointModel):
    artifact_type: str = Field(pattern=_NODE_ID_PATTERN)
    description: str = Field(min_length=1, max_length=4_000)


class PersistedWorkflowStepAcceptanceContract(_CheckpointModel):
    schema_version: Literal["workflow_step_acceptance_v2"]
    output: PersistedWorkflowArtifactContract
    criteria: tuple[PersistedWorkflowAcceptanceCriterion, ...] = Field(
        min_length=1,
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_criteria(self) -> "PersistedWorkflowStepAcceptanceContract":
        ids = tuple(item.criterion_id for item in self.criteria)
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance criterion ids must be unique")
        return self


class PersistedAdmittedWorkflowNode(_CheckpointModel):
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    kind: str = Field(min_length=1, max_length=120)
    display_title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=10_000)
    depends_on: tuple[str, ...] = Field(default=(), max_length=64)
    input_artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    acceptance_contract: PersistedWorkflowStepAcceptanceContract

    @model_validator(mode="after")
    def validate_node(self) -> "PersistedAdmittedWorkflowNode":
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("node dependencies must be unique")
        for ref in self.input_artifact_refs:
            _validate_artifact_ref(ref)
        return self


class PersistedWorkflowConstraintBinding(_CheckpointModel):
    constraint_id: str = Field(pattern=_NODE_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=4_000)
    owner_node_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    verifier_node_id: str | None = Field(default=None, pattern=_NODE_ID_PATTERN)
    severity: Literal["required", "advisory"] = "required"


class PersistedWorkflowDeliverableBinding(_CheckpointModel):
    deliverable: str = Field(min_length=1, max_length=240)
    producer_node_id: str = Field(pattern=_NODE_ID_PATTERN)


class PersistedAdmittedWorkflowPlan(_CheckpointModel):
    workflow_id: str = Field(min_length=1, max_length=512)
    version: int = Field(ge=2)
    definition_version: str = Field(min_length=1, max_length=80)
    revision_reason: str = Field(min_length=1, max_length=500)
    nodes: tuple[PersistedAdmittedWorkflowNode, ...] = Field(
        min_length=1,
        max_length=256,
    )
    constraint_bindings: tuple[PersistedWorkflowConstraintBinding, ...] = Field(
        default=(),
        max_length=64,
    )
    deliverable_bindings: tuple[PersistedWorkflowDeliverableBinding, ...] = Field(
        min_length=1,
        max_length=32,
    )
    created_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_plan_ids(self) -> "PersistedAdmittedWorkflowPlan":
        node_ids = tuple(item.node_id for item in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("admitted node ids must be unique")
        known = set(node_ids)
        if any(not set(item.depends_on).issubset(known) for item in self.nodes):
            raise ValueError("admitted plan contains unknown dependency")
        if any(item.node_id in item.depends_on for item in self.nodes):
            raise ValueError("admitted plan contains self dependency")
        if any(
            not set(item.owner_node_ids).issubset(known)
            or (
                item.verifier_node_id is not None and item.verifier_node_id not in known
            )
            for item in self.constraint_bindings
        ):
            raise ValueError("admitted constraint binding references unknown node")
        if any(
            item.producer_node_id not in known for item in self.deliverable_bindings
        ):
            raise ValueError("admitted deliverable binding references unknown node")
        return self


class PersistedWorkflowBudget(_CheckpointModel):
    model_calls_remaining: int = Field(ge=0)
    tool_calls_remaining: int = Field(ge=0)
    workflow_quanta_remaining: int = Field(ge=0)
    deadline_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_deadline(self) -> "PersistedWorkflowBudget":
        try:
            value = datetime.fromisoformat(self.deadline_at)
        except ValueError as exc:
            raise ValueError("workflow deadline must be ISO-8601") from exc
        if value.tzinfo is None:
            raise ValueError("workflow deadline must be timezone-aware")
        return self


class PersistedWorkflowBudgetSlice(_CheckpointModel):
    model_calls: int = Field(ge=0, le=10_000)
    tool_calls: int = Field(ge=0, le=100_000)
    workflow_quanta: int = Field(ge=0, le=1_000_000)


class PersistedWorkflowInputRequest(_CheckpointModel):
    required_fields: tuple[str, ...] = Field(min_length=1, max_length=32)
    prompt_code: str = Field(pattern=_NODE_ID_PATTERN)
    safe_prompt: str = Field(min_length=1, max_length=2_000)


class PersistedWorkflowResumeField(_CheckpointModel):
    name: str = Field(pattern=_NODE_ID_PATTERN)
    value: str = Field(max_length=4_000)


class WorkflowResumeInput(_CheckpointModel):
    values_by_action_ref: dict[str, dict[str, str]] = Field(
        min_length=1,
        max_length=32,
    )

    @model_validator(mode="after")
    def validate_values(self) -> "WorkflowResumeInput":
        for action_ref, fields in self.values_by_action_ref.items():
            if not action_ref or len(action_ref) > 512:
                raise ValueError("workflow resume action_ref is invalid")
            if not fields or len(fields) > 32:
                raise ValueError("workflow resume fields are invalid")
            for name, value in fields.items():
                if not re.fullmatch(_NODE_ID_PATTERN, name) or len(value) > 4_000:
                    raise ValueError("workflow resume field is invalid")
        return self


class PersistedWorkflowResumeValue(_CheckpointModel):
    action_ref: str = Field(min_length=1, max_length=512)
    fields: tuple[PersistedWorkflowResumeField, ...] = Field(
        min_length=1,
        max_length=32,
    )

    @model_validator(mode="after")
    def validate_fields(self) -> "PersistedWorkflowResumeValue":
        names = tuple(item.name for item in self.fields)
        if len(names) != len(set(names)):
            raise ValueError("resume field names must be unique")
        return self


class WorkflowBranchInterruptInput(_CheckpointModel):
    """Narrow business payload owned by the parent workflow interrupt node."""

    workflow_id: str = Field(min_length=1, max_length=512)
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    execution_generation: int = Field(ge=0, le=64)
    action_ref: str = Field(min_length=1, max_length=512)
    assignment_ref: str = Field(pattern=r"^workflow-assignment:sha256:[0-9a-f]{64}$")
    required_fields: tuple[str, ...] = Field(min_length=1, max_length=32)
    prompt_code: str = Field(pattern=_NODE_ID_PATTERN)
    safe_prompt: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_action(self) -> "WorkflowBranchInterruptInput":
        expected = stable_workflow_action_ref(
            workflow_id=self.workflow_id,
            node_id=self.node_id,
            execution_generation=self.execution_generation,
        )
        if self.action_ref != expected:
            raise ValueError("workflow interrupt action_ref does not match branch")
        if len(self.required_fields) != len(set(self.required_fields)):
            raise ValueError("workflow interrupt fields must be unique")
        if any(not re.fullmatch(_NODE_ID_PATTERN, item) for item in self.required_fields):
            raise ValueError("workflow interrupt field is invalid")
        return self


def stable_workflow_action_ref(
    *, workflow_id: str, node_id: str, execution_generation: int
) -> str:
    return (
        f"workflow:{workflow_id}:node:{node_id}:generation:{execution_generation}"
    )


class WorkflowWorkerControl(_CheckpointModel):
    outcome: Literal["completed", "blocked", "failed"]
    summary: str = Field(max_length=4_000)
    content: str = Field(default="", max_length=100_000)
    required_fields: tuple[str, ...] = Field(default=(), max_length=32)
    prompt_code: str | None = Field(default=None, max_length=160)
    safe_prompt: str | None = Field(default=None, max_length=2_000)
    error_code: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> "WorkflowWorkerControl":
        has_prompt = bool(
            self.required_fields and self.prompt_code and self.safe_prompt
        )
        if self.outcome == "completed" and (
            not self.content.strip()
            or self.required_fields
            or self.prompt_code
            or self.safe_prompt
            or self.error_code
        ):
            raise ValueError("completed control cannot carry input or error fields")
        if self.outcome == "blocked" and (
            not has_prompt or self.error_code is not None or self.content
        ):
            raise ValueError("blocked control requires only complete input fields")
        if self.outcome == "failed" and (
            self.error_code is None
            or self.required_fields
            or self.prompt_code is not None
            or self.safe_prompt is not None
            or self.content
        ):
            raise ValueError("failed control requires only error_code")
        if self.outcome == "blocked" and self.safe_prompt is not None:
            prompt = self.safe_prompt.casefold()
            unsafe_fragments = (
                "/home/",
                "/root/",
                "file://",
                "api_key",
                "access_token",
                "password=",
            )
            if any(fragment in prompt for fragment in unsafe_fragments):
                raise ValueError(
                    "blocked control prompt contains unsafe runtime detail"
                )
        return self


class WorkflowVerifierControl(_CheckpointModel):
    """Bounded structured output emitted by an AssistantTurnGraph verifier."""

    status: Literal["verified", "repair", "blocked", "failed"]
    summary: str = Field(min_length=1, max_length=4_000)
    repair_node_ids: tuple[str, ...] = Field(default=(), max_length=64)
    required_fields: tuple[str, ...] = Field(default=(), max_length=32)
    prompt_code: str | None = Field(default=None, max_length=160)
    safe_prompt: str | None = Field(default=None, max_length=2_000)
    error_code: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_status_fields(self) -> "WorkflowVerifierControl":
        if len(self.repair_node_ids) != len(set(self.repair_node_ids)):
            raise ValueError("repair node ids must be unique")
        if self.status != "repair" and self.repair_node_ids:
            raise ValueError("only repair control may carry repair node ids")
        has_prompt = bool(self.required_fields and self.prompt_code and self.safe_prompt)
        if self.status == "blocked" and not has_prompt:
            raise ValueError("blocked verifier control requires input fields")
        if self.status != "blocked" and (
            self.required_fields or self.prompt_code is not None or self.safe_prompt is not None
        ):
            raise ValueError("only blocked verifier control may request input")
        if self.status == "failed" and self.error_code is None:
            raise ValueError("failed verifier control requires error_code")
        if self.status != "failed" and self.error_code is not None:
            raise ValueError("only failed verifier control may carry error_code")
        return self


class WorkflowProfileAssignment(_CheckpointModel):
    profile: AssistantGraphProfileName
    user_id: str = Field(min_length=1, max_length=512)
    session_id: str = Field(min_length=1, max_length=512)
    agent_id: str = Field(min_length=1, max_length=512)
    workflow_id: str = Field(min_length=1, max_length=512)
    workflow_thread_id: str = Field(min_length=1, max_length=512)
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    execution_generation: int = Field(ge=0, le=64)
    assignment_ref: str = Field(pattern=r"^workflow-assignment:sha256:[0-9a-f]{64}$")
    run_id: str = Field(min_length=1, max_length=512)
    trace_id: str = Field(min_length=1, max_length=512)
    objective: str = Field(min_length=1, max_length=10_000)
    constraints: tuple[str, ...] = Field(default=(), max_length=64)
    input_artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    acceptance_contract: PersistedWorkflowStepAcceptanceContract
    capability_refs: tuple[str, ...] = Field(default=(), max_length=64)
    explicit_tool_allowlist: tuple[str, ...] = Field(default=(), max_length=256)
    available_tool_names: tuple[str, ...] = Field(default=(), max_length=256)
    tool_scope_ref: str = Field(pattern=_DIGEST_PATTERN)
    budget_slice: PersistedWorkflowBudgetSlice
    resume_value: PersistedWorkflowResumeValue | None = None
    resume_of_action_ref: str | None = Field(default=None, max_length=512)

    @classmethod
    def create(cls, **values: Any) -> "WorkflowProfileAssignment":
        payload = dict(values)
        acceptance = payload.get("acceptance_contract")
        if not isinstance(acceptance, PersistedWorkflowStepAcceptanceContract):
            payload["acceptance_contract"] = (
                PersistedWorkflowStepAcceptanceContract.model_validate_json(
                    json.dumps(acceptance)
                )
            )
        budget = payload.get("budget_slice")
        if not isinstance(budget, PersistedWorkflowBudgetSlice):
            payload["budget_slice"] = PersistedWorkflowBudgetSlice.model_validate_json(
                json.dumps(budget)
            )
        resume = payload.get("resume_value")
        if resume is not None and not isinstance(resume, PersistedWorkflowResumeValue):
            payload["resume_value"] = PersistedWorkflowResumeValue.model_validate_json(
                json.dumps(resume)
            )
        payload.setdefault("resume_value", None)
        payload.setdefault("resume_of_action_ref", None)
        payload["assignment_ref"] = _assignment_ref(payload)
        return cls.model_validate_json(
            json.dumps(payload, ensure_ascii=False, default=_json_default)
        )

    @model_validator(mode="after")
    def validate_assignment(self) -> "WorkflowProfileAssignment":
        payload = self.model_dump(mode="json", exclude={"assignment_ref"})
        if self.assignment_ref != _assignment_ref(payload):
            raise ValueError("assignment_ref does not match assignment facts")
        for ref in self.input_artifact_refs:
            _validate_artifact_ref(ref)
        for values, label in (
            (self.constraints, "constraints"),
            (self.capability_refs, "capability_refs"),
            (self.explicit_tool_allowlist, "explicit_tool_allowlist"),
            (self.available_tool_names, "available_tool_names"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if (self.resume_value is None) != (self.resume_of_action_ref is None):
            raise ValueError("resume value and action ref must be set together")
        if (
            self.resume_value is not None
            and self.resume_value.action_ref != self.resume_of_action_ref
        ):
            raise ValueError("resume action refs must match")
        if self.budget_slice.model_calls < 1:
            raise ValueError("workflow branch assignment requires model call budget")
        return self


class WorkflowBranchResult(_CheckpointModel):
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    execution_generation: int = Field(ge=0, le=64)
    profile: Literal["worker", "verifier"]
    status: Literal["succeeded", "blocked", "retryable_failed", "repair", "failed"]
    summary: str = Field(default="", max_length=4_000)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    error_code: str | None = Field(default=None, max_length=160)
    repair_node_ids: tuple[str, ...] = Field(default=(), max_length=64)
    input_request: PersistedWorkflowInputRequest | None = None
    model_calls_used: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_result(self) -> "WorkflowBranchResult":
        for ref in self.artifact_refs:
            _validate_artifact_ref(ref)
        if len(self.repair_node_ids) != len(set(self.repair_node_ids)):
            raise ValueError("repair node ids must be unique")
        if self.status == "blocked" and self.input_request is None:
            raise ValueError("blocked result requires an input request")
        if self.status != "blocked" and self.input_request is not None:
            raise ValueError("only blocked result may carry an input request")
        if self.status in {"failed", "retryable_failed"} and self.error_code is None:
            raise ValueError("failed result requires an error code")
        return self


class WorkflowResultConflict(_CheckpointModel):
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    execution_generation: int = Field(ge=0, le=64)
    variant_digests: tuple[str, str]


class WorkflowResultSlot(_CheckpointModel):
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    execution_generation: int = Field(ge=0, le=64)
    variants_by_digest: dict[str, WorkflowBranchResult] = Field(
        min_length=1,
        max_length=2,
    )
    conflict: WorkflowResultConflict | None = None

    @model_validator(mode="after")
    def validate_slot(self) -> "WorkflowResultSlot":
        expected = tuple(sorted(self.variants_by_digest))
        if any(
            result.node_id != self.node_id
            or result.execution_generation != self.execution_generation
            or _result_digest(result) != digest
            for digest, result in self.variants_by_digest.items()
        ):
            raise ValueError("result slot key or digest mismatch")
        expected_conflict = (
            WorkflowResultConflict(
                node_id=self.node_id,
                execution_generation=self.execution_generation,
                variant_digests=cast(tuple[str, str], expected),
            )
            if len(expected) == 2
            else None
        )
        if self.conflict != expected_conflict:
            raise ValueError("result slot conflict fact mismatch")
        return self


class WorkflowResultKey(_CheckpointModel):
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    execution_generation: int = Field(ge=0, le=64)

    def encode(self) -> str:
        return f"{self.node_id}:generation:{self.execution_generation}"


class WorkflowGraphError(_CheckpointModel):
    code: str = Field(pattern=_NODE_ID_PATTERN)
    message: str = Field(min_length=1, max_length=2_000)
    node_id: str | None = Field(default=None, pattern=_NODE_ID_PATTERN)
    execution_generation: int | None = Field(default=None, ge=0, le=64)


class PersistedPublishCommitRef(_CheckpointModel):
    operation_key: str = Field(min_length=1, max_length=1_024)
    status: Literal["committed"]
    result_digest: str = Field(pattern=_DIGEST_PATTERN)
    effect_ref: str = Field(min_length=1, max_length=1_024)


def _assignment_ref(values: Mapping[str, object]) -> str:
    payload = {key: value for key, value in values.items() if key != "assignment_ref"}
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode()
    return f"workflow-assignment:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", warnings=False)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _result_digest(result: WorkflowBranchResult) -> str:
    encoded = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _slot_for_results(
    results: Mapping[str, WorkflowBranchResult],
) -> WorkflowResultSlot:
    selected = dict(sorted(results.items())[:2])
    first = next(iter(selected.values()))
    digests = tuple(selected)
    conflict = (
        WorkflowResultConflict(
            node_id=first.node_id,
            execution_generation=first.execution_generation,
            variant_digests=cast(tuple[str, str], digests),
        )
        if len(digests) == 2
        else None
    )
    return WorkflowResultSlot(
        node_id=first.node_id,
        execution_generation=first.execution_generation,
        variants_by_digest=selected,
        conflict=conflict,
    )


def ledger_update(result: WorkflowBranchResult) -> dict[str, dict[str, object]]:
    key = WorkflowResultKey(
        node_id=result.node_id,
        execution_generation=result.execution_generation,
    ).encode()
    return {
        key: _slot_for_results({_result_digest(result): result}).model_dump(mode="json")
    }


def _results_from_ledger(
    value: Mapping[str, object],
) -> dict[str, dict[str, WorkflowBranchResult]]:
    collected: dict[str, dict[str, WorkflowBranchResult]] = {}
    for raw_key, raw_slot in value.items():
        try:
            # Checkpoint codecs may restore the outer registered model while
            # leaving nested values as plain mappings.  Re-validate the whole
            # JSON envelope so reducer replay never depends on Python object
            # identity or codec-specific reconstruction.
            slot = WorkflowResultSlot.model_validate_json(
                json.dumps(
                    raw_slot.model_dump(mode="json", warnings=False)
                    if isinstance(raw_slot, BaseModel)
                    else raw_slot
                )
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise WorkflowGraphStateConflict(
                f"invalid result slot at {raw_key!r}"
            ) from exc
        key = WorkflowResultKey(
            node_id=slot.node_id,
            execution_generation=slot.execution_generation,
        ).encode()
        if raw_key != key:
            raise WorkflowGraphStateConflict(
                f"result slot key {raw_key!r} does not match canonical key {key!r}"
            )
        bucket = collected.setdefault(key, {})
        bucket.update(slot.variants_by_digest)
    return collected


def merge_result_ledger(
    left: Mapping[str, object] | None,
    right: Mapping[str, object] | None,
) -> dict[str, dict[str, object]]:
    """Bounded top-two union: associative, commutative and replay-idempotent."""

    combined = _results_from_ledger(left or {})
    for key, variants in _results_from_ledger(right or {}).items():
        bucket = combined.setdefault(key, {})
        bucket.update(variants)
        combined[key] = dict(sorted(bucket.items())[:2])
    return {
        key: _slot_for_results(variants).model_dump(mode="json")
        for key, variants in sorted(combined.items())
        if variants
    }


def result_conflicts(
    ledger: Mapping[str, object],
) -> tuple[WorkflowResultConflict, ...]:
    normalized = merge_result_ledger({}, ledger)
    return tuple(
        validated.conflict
        for _, slot in sorted(normalized.items())
        if (
            validated := WorkflowResultSlot.model_validate_json(json.dumps(slot))
        ).conflict
        is not None
    )


def latest_results(
    ledger: Mapping[str, object],
    generations: Mapping[str, int],
) -> dict[str, WorkflowBranchResult]:
    normalized = merge_result_ledger({}, ledger)
    conflicts = result_conflicts(normalized)
    if conflicts:
        conflict = conflicts[0]
        raise WorkflowGraphStateConflict(
            f"node {conflict.node_id} generation {conflict.execution_generation} "
            "has conflicting branch results"
        )
    result: dict[str, WorkflowBranchResult] = {}
    for node_id, generation in sorted(generations.items()):
        key = WorkflowResultKey(
            node_id=node_id,
            execution_generation=generation,
        ).encode()
        slot = normalized.get(key)
        if slot is not None:
            validated = WorkflowResultSlot.model_validate_json(json.dumps(slot))
            result[node_id] = next(iter(validated.variants_by_digest.values()))
    return result


def _resume_value(value: object) -> PersistedWorkflowResumeValue | None:
    try:
        payload = (
            value.model_dump(mode="json", warnings=False)
            if isinstance(value, PersistedWorkflowResumeValue)
            else value
        )
        return PersistedWorkflowResumeValue.model_validate_json(json.dumps(payload))
    except (TypeError, ValueError, ValidationError):
        return None


def merge_resume_values(
    left: Mapping[str, object] | None,
    right: Mapping[str, object] | None,
) -> dict[str, PersistedWorkflowResumeValue]:
    candidates: dict[str, dict[str, PersistedWorkflowResumeValue]] = {}
    for source in (left or {}, right or {}):
        for key, raw in source.items():
            value = _resume_value(raw)
            if value is None or key != value.action_ref:
                continue
            digest = hashlib.sha256(value.model_dump_json().encode()).hexdigest()
            candidates.setdefault(key, {})[digest] = value
    return {
        key: values[min(values)] for key, values in sorted(candidates.items()) if values
    }


def merge_sorted_unique_refs(
    left: tuple[str, ...] | list[str] | None,
    right: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    return tuple(sorted(set(left or ()) | set(right or ())))[:1_000]


def _graph_error(value: object) -> WorkflowGraphError | None:
    try:
        return (
            value
            if isinstance(value, WorkflowGraphError)
            else WorkflowGraphError.model_validate(value)
        )
    except (TypeError, ValueError, ValidationError):
        return None


def merge_graph_errors(
    left: tuple[object, ...] | list[object] | None,
    right: tuple[object, ...] | list[object] | None,
) -> tuple[dict[str, object], ...]:
    values: dict[str, WorkflowGraphError] = {}
    for raw in (*tuple(left or ()), *tuple(right or ())):
        error = _graph_error(raw)
        if error is None:
            continue
        digest = hashlib.sha256(error.model_dump_json().encode()).hexdigest()
        values[digest] = error
    return tuple(values[key].model_dump(mode="json") for key in sorted(values))[:256]


class DurableWorkflowState(TypedDict):
    graph_name: Literal["DurableWorkflowGraph"]
    graph_version: Literal["3"]
    state_schema_version: Literal[1]
    execution_engine: Literal["langgraph_v3"]
    workflow_id: str
    workflow_type: Literal["deep_research"]
    identity: PersistedWorkflowIdentity
    workflow_thread_id: str
    invocation_run_id: str
    invocation_trace_id: str
    invocation_run_ids: Annotated[tuple[str, ...], merge_sorted_unique_refs]
    definition_version: str
    current_plan_version: int
    submission: PersistedWorkflowSubmission
    admitted_plan: PersistedAdmittedWorkflowPlan | None
    status: WorkflowGraphStatus
    phase: WorkflowPhase
    execution_generation_by_node: dict[str, int]
    active_wave: tuple[WorkflowProfileAssignment, ...]
    wave_history: tuple[tuple[str, ...], ...]
    result_ledger: Annotated[dict[str, WorkflowResultSlot], merge_result_ledger]
    resume_values_by_action_ref: Annotated[
        dict[str, PersistedWorkflowResumeValue], merge_resume_values
    ]
    consumed_action_refs: Annotated[tuple[str, ...], merge_sorted_unique_refs]
    repair_round: int
    budget: PersistedWorkflowBudget
    result_artifact_refs: tuple[str, ...]
    publish_commit_ref: PersistedPublishCommitRef | None
    errors: Annotated[tuple[WorkflowGraphError, ...], merge_graph_errors]


class _DurableWorkflowStateModel(_CheckpointModel):
    graph_name: Literal["DurableWorkflowGraph"]
    graph_version: Literal["3"]
    state_schema_version: Literal[1]
    execution_engine: Literal["langgraph_v3"]
    workflow_id: str = Field(min_length=1, max_length=512)
    workflow_type: Literal["deep_research"]
    identity: PersistedWorkflowIdentity
    workflow_thread_id: str = Field(min_length=1, max_length=512)
    invocation_run_id: str = Field(min_length=1, max_length=512)
    invocation_trace_id: str = Field(min_length=1, max_length=512)
    invocation_run_ids: tuple[str, ...] = Field(min_length=1, max_length=1_000)
    definition_version: str = Field(min_length=1, max_length=80)
    current_plan_version: int = Field(ge=1)
    submission: PersistedWorkflowSubmission
    admitted_plan: PersistedAdmittedWorkflowPlan | None
    status: WorkflowGraphStatus
    phase: WorkflowPhase
    execution_generation_by_node: dict[str, int] = Field(max_length=256)
    active_wave: tuple[WorkflowProfileAssignment, ...] = Field(
        default=(), max_length=256
    )
    wave_history: tuple[tuple[str, ...], ...] = Field(default=(), max_length=256)
    result_ledger: dict[str, WorkflowResultSlot] = Field(
        default_factory=dict, max_length=16_640
    )
    resume_values_by_action_ref: dict[str, PersistedWorkflowResumeValue] = Field(
        default_factory=dict,
        max_length=1_000,
    )
    consumed_action_refs: tuple[str, ...] = Field(default=(), max_length=1_000)
    repair_round: int = Field(default=0, ge=0, le=64)
    budget: PersistedWorkflowBudget
    result_artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    publish_commit_ref: PersistedPublishCommitRef | None = None
    errors: tuple[WorkflowGraphError, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "_DurableWorkflowStateModel":
        if self.identity.workflow_thread_id != self.workflow_thread_id:
            raise ValueError("workflow identity thread mismatch")
        if self.invocation_run_id not in self.invocation_run_ids:
            raise ValueError("current invocation run must be in invocation ledger")
        if self.submission.workflow_type != self.workflow_type:
            raise ValueError("submission workflow type mismatch")
        if self.admitted_plan is not None:
            if self.admitted_plan.workflow_id != self.workflow_id:
                raise ValueError("admitted plan workflow identity mismatch")
            node_ids = {node.node_id for node in self.admitted_plan.nodes}
            if set(self.execution_generation_by_node) != node_ids:
                raise ValueError("execution generation map must cover admitted nodes")
        elif self.execution_generation_by_node:
            raise ValueError("planning state cannot contain node generations")
        for node_id, generation in self.execution_generation_by_node.items():
            if not re.fullmatch(_NODE_ID_PATTERN, node_id) or not 0 <= generation <= 64:
                raise ValueError("invalid execution generation entry")
        for ref in self.result_artifact_refs:
            _validate_artifact_ref(ref)
        for wave in self.wave_history:
            if not wave or len(wave) > 256 or tuple(sorted(set(wave))) != wave:
                raise ValueError("wave history entries must be sorted unique node ids")
            if any(not re.fullmatch(_NODE_ID_PATTERN, node_id) for node_id in wave):
                raise ValueError("wave history contains invalid node id")
        normalized = merge_result_ledger({}, self.result_ledger)
        serialized_ledger = {
            key: value.model_dump(mode="json")
            for key, value in self.result_ledger.items()
        }
        if normalized != serialized_ledger:
            raise ValueError("result ledger is not canonical")
        serialized_resumes = {
            key: value.model_dump(mode="json")
            for key, value in self.resume_values_by_action_ref.items()
        }
        normalized_resumes = {
            key: value.model_dump(mode="json")
            for key, value in merge_resume_values(
                {}, self.resume_values_by_action_ref
            ).items()
        }
        if normalized_resumes != serialized_resumes:
            raise ValueError("resume ledger is not canonical")
        if (
            merge_sorted_unique_refs((), self.consumed_action_refs)
            != self.consumed_action_refs
        ):
            raise ValueError("consumed action refs are not canonical")
        if merge_graph_errors((), self.errors) != tuple(
            item.model_dump(mode="json") for item in self.errors
        ):
            raise ValueError("graph errors are not canonical")
        return self


_STATE_ADAPTER = TypeAdapter(_DurableWorkflowStateModel)


def _persist_submission(value: WorkflowSubmission) -> PersistedWorkflowSubmission:
    questions = value.inputs.get("research_questions")
    if not isinstance(questions, list):
        raise ValueError("research_questions must be a list")
    return PersistedWorkflowSubmission(
        workflow_type=cast(Literal["deep_research"], value.workflow_type),
        objective=value.objective,
        deliverables=tuple(value.deliverables),
        constraints=tuple(value.constraints),
        inputs=PersistedDeepResearchInputs(research_questions=tuple(questions)),
        requested_budget=PersistedWorkflowBudgetRequest.model_validate(
            value.requested_budget.model_dump(mode="python")
        ),
        durability_reasons=tuple(value.durability_reasons),
        seed_artifact_refs=tuple(value.seed_artifact_refs),
        idempotency_key=value.idempotency_key,
    )


def persist_admitted_workflow_plan(
    value: WorkflowPlanVersion,
) -> PersistedAdmittedWorkflowPlan:
    nodes = tuple(
        PersistedAdmittedWorkflowNode(
            node_id=item.work_item_id,
            kind=item.kind,
            display_title=item.display_title,
            objective=item.objective,
            depends_on=tuple(item.depends_on),
            input_artifact_refs=tuple(item.input_artifact_refs),
            acceptance_contract=(
                item.acceptance_contract
                if isinstance(
                    item.acceptance_contract,
                    PersistedWorkflowStepAcceptanceContract,
                )
                else PersistedWorkflowStepAcceptanceContract.model_validate_json(
                    json.dumps(
                        item.acceptance_contract.model_dump(mode="json")
                        if isinstance(item.acceptance_contract, BaseModel)
                        else item.acceptance_contract
                    )
                )
            ),
        )
        for item in value.work_items
    )
    return PersistedAdmittedWorkflowPlan(
        workflow_id=value.workflow_id,
        version=value.version,
        definition_version=value.definition_version,
        revision_reason=value.revision_reason,
        nodes=nodes,
        constraint_bindings=tuple(
            PersistedWorkflowConstraintBinding(
                constraint_id=item.constraint_id,
                statement=item.statement,
                owner_node_ids=tuple(item.owner_work_item_ids),
                verifier_node_id=item.verifier_work_item_id,
                severity=item.severity,
            )
            for item in value.constraint_bindings
        ),
        deliverable_bindings=tuple(
            PersistedWorkflowDeliverableBinding(
                deliverable=item.deliverable,
                producer_node_id=item.producer_work_item_id,
            )
            for item in value.deliverable_bindings
        ),
        created_at=value.created_at.isoformat(),
    )


def _persist_budget(value: WorkflowBudget) -> PersistedWorkflowBudget:
    return PersistedWorkflowBudget(
        model_calls_remaining=value.model_calls_remaining,
        tool_calls_remaining=value.tool_calls_remaining,
        workflow_quanta_remaining=value.workflow_quanta_remaining,
        deadline_at=value.deadline_at.isoformat(),
    )


def initial_workflow_graph_state(
    *,
    workflow: WorkflowRecord,
    submission: WorkflowSubmission,
    admitted_plan: WorkflowPlanVersion | None,
    workflow_thread_id: str,
    invocation_run_id: str,
    invocation_trace_id: str,
) -> DurableWorkflowState:
    if workflow.execution_engine != "langgraph_v3":
        raise ValueError(
            f"graph app rejects workflow engine {workflow.execution_engine}"
        )
    if (
        workflow.workflow_type != "deep_research"
        or submission.workflow_type != "deep_research"
    ):
        raise ValueError("DurableWorkflowGraph only accepts deep_research")
    if workflow.workflow_id != (
        admitted_plan.workflow_id if admitted_plan else workflow.workflow_id
    ):
        raise ValueError("admitted plan references another workflow")
    persisted_plan = (
        persist_admitted_workflow_plan(admitted_plan)
        if admitted_plan is not None
        else None
    )
    phase: WorkflowPhase = "executing" if persisted_plan is not None else "planning"
    envelope = _DurableWorkflowStateModel(
        graph_name=WORKFLOW_GRAPH_NAME,
        graph_version=WORKFLOW_GRAPH_VERSION,
        state_schema_version=WORKFLOW_STATE_SCHEMA_VERSION,
        execution_engine="langgraph_v3",
        workflow_id=workflow.workflow_id,
        workflow_type="deep_research",
        identity=PersistedWorkflowIdentity(
            user_id=workflow.user_id,
            session_id=workflow.session_id,
            agent_id=workflow.agent_id,
            workflow_thread_id=workflow_thread_id,
            turn_origin_id=workflow.ingress_run_id,
        ),
        workflow_thread_id=workflow_thread_id,
        invocation_run_id=invocation_run_id,
        invocation_trace_id=invocation_trace_id,
        invocation_run_ids=(invocation_run_id,),
        definition_version=workflow.definition_version,
        current_plan_version=workflow.current_plan_version,
        submission=_persist_submission(submission),
        admitted_plan=persisted_plan,
        status=cast(WorkflowGraphStatus, workflow.status),
        phase=phase,
        execution_generation_by_node=(
            {node.node_id: 0 for node in persisted_plan.nodes}
            if persisted_plan is not None
            else {}
        ),
        active_wave=(),
        wave_history=(),
        result_ledger={},
        resume_values_by_action_ref={},
        consumed_action_refs=(),
        repair_round=0,
        budget=_persist_budget(workflow.budget),
        result_artifact_refs=tuple(workflow.result_artifact_refs),
        publish_commit_ref=None,
        errors=(),
    )
    return cast(DurableWorkflowState, envelope.model_dump(mode="json"))


def validate_durable_workflow_state(
    value: Mapping[str, object],
) -> DurableWorkflowState:
    if (
        value.get("graph_name") != WORKFLOW_GRAPH_NAME
        or value.get("graph_version") != WORKFLOW_GRAPH_VERSION
        or value.get("state_schema_version") != WORKFLOW_STATE_SCHEMA_VERSION
        or value.get("execution_engine") != "langgraph_v3"
    ):
        raise WorkflowGraphStateCompatibilityError()
    try:
        validated = _STATE_ADAPTER.validate_json(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                default=_json_default,
            )
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("workflow_graph_state_invalid") from exc
    return cast(DurableWorkflowState, validated.model_dump(mode="json"))


__all__ = [
    "DurableWorkflowState",
    "PersistedAdmittedWorkflowPlan",
    "PersistedWorkflowBudget",
    "PersistedWorkflowBudgetSlice",
    "PersistedWorkflowIdentity",
    "PersistedWorkflowInputRequest",
    "PersistedWorkflowResumeValue",
    "PersistedWorkflowStepAcceptanceContract",
    "WorkflowBranchResult",
    "WorkflowGraphError",
    "WorkflowGraphStateCompatibilityError",
    "WorkflowGraphStateConflict",
    "WorkflowProfileAssignment",
    "WorkflowResultConflict",
    "WorkflowResultKey",
    "WorkflowResultSlot",
    "WorkflowWorkerControl",
    "WorkflowVerifierControl",
    "initial_workflow_graph_state",
    "latest_results",
    "ledger_update",
    "merge_graph_errors",
    "merge_result_ledger",
    "merge_resume_values",
    "merge_sorted_unique_refs",
    "persist_admitted_workflow_plan",
    "result_conflicts",
    "validate_durable_workflow_state",
]

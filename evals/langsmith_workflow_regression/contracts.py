"""Strict, bounded Dataset contracts for Durable Workflow experiments."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


WORKFLOW_REGRESSION_DATASET = "assistant-agent-durable-workflow-regressions"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowBudgetInput(_StrictModel):
    model_calls: int = Field(ge=1, le=10_000)
    tool_calls: int = Field(ge=1, le=100_000)
    workflow_quanta: int = Field(ge=1, le=1_000_000)
    deadline_seconds: int = Field(ge=60, le=2_592_000)


class DeepResearchInputs(_StrictModel):
    schema_version: Literal["deep_research_inputs_v2"]
    research_questions: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_questions(self) -> "DeepResearchInputs":
        if any(not item.strip() or len(item) > 4_000 for item in self.research_questions):
            raise ValueError("research questions must be bounded and non-empty")
        return self


class WorkflowSubmissionInput(_StrictModel):
    workflow_type: Literal["deep_research"]
    objective: str = Field(min_length=1, max_length=10_000)
    deliverables: tuple[str, ...] = Field(min_length=1, max_length=32)
    constraints: tuple[str, ...] = Field(min_length=1, max_length=64)
    inputs: DeepResearchInputs
    requested_budget: WorkflowBudgetInput
    durability_reasons: tuple[str, ...] = Field(min_length=1, max_length=16)
    seed_artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_text(self) -> "WorkflowSubmissionInput":
        values = (*self.deliverables, *self.constraints, *self.durability_reasons)
        if any(not value.strip() or len(value) > 4_000 for value in values):
            raise ValueError("submission facts must be bounded and non-empty")
        return self


class WorkflowExampleInput(_StrictModel):
    schema_version: Literal["workflow_experiment_input_v1"]
    case_type: Literal[
        "parallel_join",
        "constraint_verifier",
        "minimal_repair",
        "interrupt_resume_equivalence",
    ]
    workflow_id: str = Field(min_length=1, max_length=512)
    submission: WorkflowSubmissionInput
    truncated: Literal[False]


class ExpectedPlan(_StrictModel):
    node_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    dependencies: dict[str, tuple[str, ...]] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_plan(self) -> "ExpectedPlan":
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("expected plan node ids must be unique")
        known = set(self.node_ids)
        if set(self.dependencies) != known:
            raise ValueError("expected dependencies must cover every node")
        if any(not set(parents).issubset(known) for parents in self.dependencies.values()):
            raise ValueError("expected plan contains unknown dependency")
        return self


class WorkflowEvaluationContract(_StrictModel):
    deliverables: dict[str, str] = Field(min_length=1, max_length=32)
    constraint_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    repair_scope: tuple[str, ...] = Field(default=(), max_length=64)
    resume_equivalent: bool


class WorkflowReferenceOutput(_StrictModel):
    terminal_status: Literal["completed", "failed", "cancelled", "interrupted"]
    plan: ExpectedPlan
    evaluation_contract: WorkflowEvaluationContract


class WorkflowExampleMetadata(_StrictModel):
    active: bool
    risk: Literal["critical", "high", "medium", "low"]
    source_trace_id: str | None = Field(default=None, min_length=1, max_length=512)
    owner: Literal["git:assistant_agent"] | None = None
    case_id: str | None = Field(default=None, min_length=1, max_length=160)
    git_commit: str | None = Field(default=None, min_length=1, max_length=160)


class WorkflowDatasetExample(_StrictModel):
    id: str = Field(min_length=1, max_length=512)
    inputs: WorkflowExampleInput
    outputs: WorkflowReferenceOutput
    metadata: WorkflowExampleMetadata


def validate_active_examples(values: list[object]) -> tuple[WorkflowDatasetExample, ...]:
    examples = tuple(WorkflowDatasetExample.model_validate(value) for value in values)
    active = tuple(example for example in examples if example.metadata.active)
    if not active:
        raise RuntimeError("workflow regression Dataset has no active examples")
    return active


__all__ = [
    "WORKFLOW_REGRESSION_DATASET",
    "WorkflowDatasetExample",
    "WorkflowReferenceOutput",
    "validate_active_examples",
]

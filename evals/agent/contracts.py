"""Small contracts shared by Agent eval tasks, environments, and graders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.observability.trace_store import TraceEvent
from assistant_agent.runtime.requests import UserRequest


class TaskSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    request: UserRequest
    environment: str = Field(min_length=1)
    grader: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class ToolExecution(BaseModel):
    tool_call_id: str | None = None
    name: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(min_length=1)
    terminal_event: str | None = None
    exposed: bool = False
    error_code: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    tool_name: str | None = None
    status: str | None = None
    tool_call_id: str | None = None


class ToolOutcomeExpectation(BaseModel):
    tool_name: str = Field(min_length=1)
    required: bool = True
    expected_result: Literal["success", "failure"]
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_expected_result(self) -> ToolOutcomeExpectation:
        if self.expected_result == "success" and self.error_code is not None:
            raise ValueError("A successful tool outcome cannot declare an error_code.")
        if self.expected_result == "failure" and not self.error_code:
            raise ValueError(
                "A failed tool outcome must declare the expected error_code."
            )
        return self

    @classmethod
    def must_succeed(cls, tool_name: str) -> ToolOutcomeExpectation:
        return cls(
            tool_name=tool_name,
            expected_result="success",
        )

    @classmethod
    def must_fail_with(
        cls,
        tool_name: str,
        *,
        error_code: str,
    ) -> ToolOutcomeExpectation:
        return cls(
            tool_name=tool_name,
            expected_result="failure",
            error_code=error_code,
        )


class RunEvidence(BaseModel):
    schema_version: Literal["agent_eval_run_evidence_v1"] = "agent_eval_run_evidence_v1"
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    terminal_status: str = Field(min_length=1)
    response: dict[str, Any] | None = None
    available_tools: list[str] = Field(default_factory=list)
    tool_executions: list[ToolExecution] = Field(default_factory=list)
    validation_results: list[ValidationResult] = Field(default_factory=list)
    initial_state: dict[str, Any] = Field(default_factory=dict)
    final_state: dict[str, Any] = Field(default_factory=dict)
    state_diff: dict[str, Any] = Field(default_factory=dict)
    trace_event_names: list[str] = Field(default_factory=list)
    provider_result_kinds: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class TaskExecution:
    evidence: RunEvidence
    trace_events: list[TraceEvent]


class AssertionResult(BaseModel):
    passed: bool
    label: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1)
    evaluation_method: Literal["rule", "judge"]
    criterion_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_]*$",
    )

    @model_validator(mode="after")
    def validate_evaluation_provenance(self) -> AssertionResult:
        if self.evaluation_method == "rule" and self.criterion_id is not None:
            raise ValueError("A rule assertion cannot declare a criterion_id.")
        if self.evaluation_method == "judge" and not self.criterion_id:
            raise ValueError("A judge assertion must declare a criterion_id.")
        return self


class DimensionResult(BaseModel):
    passed: bool
    reason: str = Field(min_length=1)
    assertions: dict[str, AssertionResult] = Field(min_length=1)


class GraderDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_execution: DimensionResult
    tool_semantics: DimensionResult
    grounding: DimensionResult
    response_quality: DimensionResult


class TaskJudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_semantics: DimensionResult
    grounding: DimensionResult
    response_quality: DimensionResult

    @model_validator(mode="after")
    def validate_dimension_assertion_provenance(self) -> TaskJudgeResult:
        for criterion_id in (
            "tool_semantics",
            "grounding",
            "response_quality",
        ):
            dimension_result = getattr(self, criterion_id)
            for assertion in dimension_result.assertions.values():
                if assertion.evaluation_method != "judge":
                    raise ValueError(
                        f"{criterion_id} must contain only Judge assertions."
                    )
                if assertion.criterion_id != criterion_id:
                    raise ValueError(
                        f"{criterion_id} assertion criterion_id must be "
                        f"{criterion_id!r}."
                    )
        return self


class EnvironmentValidation(BaseModel):
    schema_version: Literal["agent_eval_environment_validation_v2"] = (
        "agent_eval_environment_validation_v2"
    )
    passed: bool
    reason: str = Field(min_length=1)
    checks: dict[str, AssertionResult] = Field(min_length=1)

    def require_valid(self) -> None:
        if not self.passed:
            raise RuntimeError(
                "Agent eval Environment validation failed: " + self.reason
            )


class JudgeVerdict(BaseModel):
    passed: bool
    reason: str = Field(min_length=1)


class GraderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent_eval_grader_result_v5"] = (
        "agent_eval_grader_result_v5"
    )
    dimensions: GraderDimensions


class LLMJudge(Protocol):
    def evaluate(
        self,
        *,
        criterion_id: str,
        rubric: str,
        evidence: RunEvidence,
    ) -> JudgeVerdict: ...


class TaskEnvironment(Protocol):
    def describe(self) -> dict[str, Any]: ...

    def validate(self) -> EnvironmentValidation: ...

    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]: ...

    def execute(
        self,
        *,
        task: TaskSpec,
        request: UserRequest,
        trace_id: str,
        parent_span_id: str,
    ) -> TaskExecution: ...

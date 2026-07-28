"""Small contracts shared by Agent eval tasks, environments, and graders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

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


class CheckResult(BaseModel):
    passed: bool
    reason: str = Field(min_length=1)


class SemanticVerdict(BaseModel):
    passed: bool
    reason: str = Field(min_length=1)


class GraderResult(BaseModel):
    schema_version: Literal["agent_eval_grader_result_v1"] = (
        "agent_eval_grader_result_v1"
    )
    passed: bool
    reward: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    checks: dict[str, CheckResult]


class SemanticJudge(Protocol):
    def evaluate(
        self,
        *,
        criterion: str,
        evidence: RunEvidence,
    ) -> SemanticVerdict: ...


class TaskEnvironment(Protocol):
    def describe(self) -> dict[str, Any]: ...

    def execute(
        self,
        *,
        task: TaskSpec,
        request: UserRequest,
        trace_id: str,
        parent_span_id: str,
    ) -> TaskExecution: ...

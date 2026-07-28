"""Stable schemas and protocols for Langfuse Agent experiments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from evals.langfuse.weather_failure_fixture import WeatherFailureFixture


EvaluationScoreName = Literal[
    "agent.runtime_trace_pass",
    "agent.tool_mechanical_pass",
    "agent.tool_semantic_pass",
    "agent.answer_semantic_pass",
]


class ScoreEvidenceRequirement(BaseModel):
    evidence: list[str] = Field(min_length=1)
    pass_condition: str = Field(min_length=1)


class CaseEvaluationContract(BaseModel):
    schema_version: Literal[
        "assistant_agent_case_evaluation_contract_v1"
    ] = "assistant_agent_case_evaluation_contract_v1"
    pass_iff: str = Field(min_length=1)
    evidence_by_score: dict[
        EvaluationScoreName,
        ScoreEvidenceRequirement,
    ]

    @model_validator(mode="after")
    def require_all_score_layers(self) -> "CaseEvaluationContract":
        required_scores = set(EvaluationScoreName.__args__)
        actual_scores = set(self.evidence_by_score)
        if actual_scores != required_scores:
            missing = sorted(required_scores - actual_scores)
            unexpected = sorted(actual_scores - required_scores)
            raise ValueError(
                "evaluation_contract must define exactly the four score "
                f"layers; missing={missing}, unexpected={unexpected}."
            )
        return self


class EngineeredCaseInputV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_request: dict[str, Any]


class CaseDependencyV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    type: Literal[
        "frozen_fixture",
        "isolated_state",
        "injected_failure",
        "live_service",
    ]
    description: str = Field(min_length=1)
    fixture_id: str | None = None
    uses_live_external_service: bool
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_fixture_for_controlled_dependency(self) -> "CaseDependencyV2":
        if self.type != "live_service" and not self.fixture_id:
            raise ValueError(
                f"dependency type {self.type!r} requires fixture_id."
            )
        if self.type == "live_service" and not self.uses_live_external_service:
            raise ValueError(
                "live_service dependency must set "
                "uses_live_external_service=true."
            )
        if self.type != "live_service" and self.uses_live_external_service:
            raise ValueError(
                f"controlled dependency type {self.type!r} must set "
                "uses_live_external_service=false."
            )
        return self


class EngineeredCaseMetadataV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "assistant_agent_case_metadata_v2"
    ] = "assistant_agent_case_metadata_v2"
    capability: str = Field(min_length=1)
    scenario_summary: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    lifecycle: Literal["draft", "calibrated", "active", "retired"]
    compatible_profiles: list[
        Literal["real_readonly", "real_system"]
    ] = Field(min_length=1)
    dependencies: list[CaseDependencyV2] = Field(min_length=1)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    effect_scope: str = Field(min_length=1)
    calibration_fixture: str = Field(min_length=1)


class CaseOracleV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "assistant_agent_case_oracle_v2"
    ] = "assistant_agent_case_oracle_v2"
    type: Literal["grounded_facts", "state_invariant", "injected_failure"]
    description: str = Field(min_length=1)
    fixture: dict[str, Any] | None
    ground_truth: dict[str, Any]
    required_facts: list[str]
    forbidden_facts: list[str]
    state_constraints: dict[str, Any]


class EngineeredCaseExpectationV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "assistant_agent_case_expectation_v2"
    ] = "assistant_agent_case_expectation_v2"
    evaluation_contract: CaseEvaluationContract
    oracle: CaseOracleV2


class DatasetSeedItem(BaseModel):
    id: str = Field(min_length=1)
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_evaluation_contract(self) -> "DatasetSeedItem":
        contract = self.expected_output.get("evaluation_contract")
        if contract is not None:
            CaseEvaluationContract.model_validate(contract)
        return self


class DatasetCaseCollection(BaseModel):
    schema_version: Literal[
        "assistant_agent_eval_case_collection_v1",
        "assistant_agent_eval_case_collection_v2",
    ] = "assistant_agent_eval_case_collection_v1"
    group: Literal["legacy", "engineered"]
    items: list[DatasetSeedItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_group_schema(self) -> "DatasetCaseCollection":
        if self.group == "legacy":
            if self.schema_version != "assistant_agent_eval_case_collection_v1":
                raise ValueError("legacy collections must use collection_v1.")
            return self
        if self.schema_version != "assistant_agent_eval_case_collection_v2":
            raise ValueError("engineered collections must use collection_v2.")
        for item in self.items:
            EngineeredCaseInputV2.model_validate(item.input)
            EngineeredCaseExpectationV2.model_validate(item.expected_output)
            EngineeredCaseMetadataV2.model_validate(item.metadata)
        return self


class DatasetSeedComposition(BaseModel):
    schema_version: Literal[
        "assistant_agent_eval_dataset_composition_v1"
    ] = "assistant_agent_eval_dataset_composition_v1"
    dataset_name: str = Field(min_length=1)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    case_sources: list[Path] = Field(min_length=1)


class DatasetSeed(BaseModel):
    dataset_name: str = Field(min_length=1)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    items: list[DatasetSeedItem] = Field(min_length=1)

    def content_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class DatasetSeedResult(BaseModel):
    dataset_name: str
    seed_hash: str
    item_ids: list[str]
    removed_item_ids: list[str] = Field(default_factory=list)


class CalendarEventExpectation(BaseModel):
    title: str = Field(min_length=1)
    start_time: str = Field(min_length=1)
    end_time: str | None = None
    timezone: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list)
    notes: str | None = None


class CreateCalendarCase(BaseModel):
    id: str = Field(min_length=1)
    required_event: CalendarEventExpectation
    response_facts: list[str] = Field(default_factory=list)


class ReadCalendarCase(BaseModel):
    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    response_facts: list[str] = Field(default_factory=list)


class NoToolCase(BaseModel):
    id: str = Field(min_length=1)
    response_facts: list[str] = Field(default_factory=list)


class FrozenFileFixture(BaseModel):
    source_path: str = Field(min_length=1)
    target_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RealAgentCase(BaseModel):
    id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    required_tools: list[str] = Field(default_factory=list)
    response_facts: list[str] = Field(default_factory=list)
    weather_failure: WeatherFailureFixture | None = None
    frozen_file: FrozenFileFixture | None = None


ExperimentCase = (
    CreateCalendarCase | ReadCalendarCase | NoToolCase | RealAgentCase
)


class AgentExperimentOutput(BaseModel):
    schema_version: str = "agent_experiment_output_v1"
    case_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    terminal_status: str = Field(min_length=1)
    response: dict[str, Any] | None = None
    available_tools: list[str] = Field(default_factory=list)
    request_metadata: dict[str, Any] = Field(default_factory=dict)
    tool_executions: list[dict[str, Any]] = Field(default_factory=list)
    validation_results: list[dict[str, Any]] = Field(default_factory=list)
    initial_state: dict[str, Any] = Field(default_factory=dict)
    final_state: dict[str, Any] = Field(default_factory=dict)
    state_diff: dict[str, Any] = Field(default_factory=dict)
    trace_event_names: list[str] = Field(default_factory=list)
    provider_result_kinds: list[str] = Field(default_factory=list)
    total_latency_ms: int = Field(default=0, ge=0)
    execution_error: dict[str, str] | None = None


class LangfuseExperimentClient(Protocol):
    def get_current_trace_id(self) -> str | None: ...

    def get_current_observation_id(self) -> str | None: ...


class RuntimeTraceObserver(Protocol):
    def on_trace_event(self, event: Any) -> None: ...

    def close(self, *, timeout: float) -> bool: ...


class EvalStateEnvironment(Protocol):
    def snapshot(self) -> dict[str, Any]: ...

    def diff(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RuntimeBundle:
    runtime: AgentGraphRuntime
    environment: EvalStateEnvironment


RuntimeFactory = Callable[[UserRequest, ExperimentCase], RuntimeBundle]


class StatelessEvalEnvironment:
    def snapshot(self) -> dict[str, Any]:
        return {}

    def diff(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "added": [],
            "modified": [],
            "deleted": [],
            "duplicate_groups": [],
        }

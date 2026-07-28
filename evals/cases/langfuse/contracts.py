"""Stable schemas and protocols for Langfuse Agent experiments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from evals.cases.langfuse.weather_failure_fixture import WeatherFailureFixture


class DatasetSeedItem(BaseModel):
    id: str = Field(min_length=1)
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


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

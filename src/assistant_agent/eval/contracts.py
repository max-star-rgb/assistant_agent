"""Contracts shared by trace-driven agent evaluations."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import AgentResponse
from assistant_agent.services.trace_store import TraceEvent


class AgentEvalEvidence(BaseModel):
    """Complete evidence returned by one experiment task."""

    schema_version: Literal["agent_eval_evidence_v1"] = "agent_eval_evidence_v1"
    case_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    terminal_status: str = Field(min_length=1)
    response: AgentResponse | None = None
    trace_events: list[TraceEvent] = Field(default_factory=list)
    initial_state: dict[str, Any] = Field(default_factory=dict)
    final_state: dict[str, Any] = Field(default_factory=dict)
    state_diff: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    runtime_metadata: dict[str, Any] = Field(default_factory=dict)


class AgentEvalScore(BaseModel):
    """Framework-neutral score that can be adapted to a Langfuse Evaluation."""

    name: str = Field(min_length=1)
    value: float
    data_type: Literal["BOOLEAN", "NUMERIC"] = "NUMERIC"
    comment: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class AgentEvalReport(BaseModel):
    """All item-level scores for one evaluated rollout."""

    schema_version: Literal["agent_eval_report_v1"] = "agent_eval_report_v1"
    case_id: str = Field(min_length=1)
    scores: list[AgentEvalScore] = Field(default_factory=list)

    def score(self, name: str) -> AgentEvalScore:
        """Return one score by its stable Langfuse-facing name."""

        try:
            return next(score for score in self.scores if score.name == name)
        except StopIteration as exc:
            raise KeyError(f"Unknown eval score: {name}") from exc


def evidence_from_runtime_state(
    *,
    case_id: str,
    state: AgentState,
    trace_events: list[TraceEvent],
    initial_state: dict[str, Any],
    final_state: dict[str, Any],
    state_diff: dict[str, Any],
    runtime_metadata: dict[str, Any] | None = None,
) -> AgentEvalEvidence:
    """Build the experiment output without coupling Runtime to an eval framework."""

    metadata = dict(runtime_metadata or {})
    metadata.setdefault(
        "available_tool_names",
        (
            list(state.run_tool_catalog.available_tool_names)
            if state.run_tool_catalog is not None
            else []
        ),
    )
    metadata.setdefault("request_metadata", dict(state.request.metadata))
    metadata.setdefault(
        "tool_calls",
        [call.model_dump(mode="json") for call in state.tool_calls],
    )
    metadata.setdefault(
        "tool_results",
        [result.model_dump(mode="json") for result in state.tool_results],
    )
    return AgentEvalEvidence(
        case_id=case_id,
        run_id=state.run_id,
        trace_id=state.trace_id,
        terminal_status=state.status,
        response=state.response,
        trace_events=trace_events,
        initial_state=initial_state,
        final_state=final_state,
        state_diff=state_diff,
        usage=_usage_from_trace(trace_events),
        runtime_metadata=metadata,
    )


def _usage_from_trace(trace_events: list[TraceEvent]) -> dict[str, int]:
    totals = {"input": 0, "output": 0, "total": 0}
    for event in trace_events:
        usage = event.attributes.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, aliases in {
            "input": ("input_tokens", "prompt_tokens"),
            "output": ("output_tokens", "completion_tokens"),
            "total": ("total_tokens", "token_count"),
        }.items():
            value = next(
                (
                    usage[alias]
                    for alias in aliases
                    if alias in usage
                ),
                None,
            )
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[key] += value
    if totals["total"] == 0 and (totals["input"] or totals["output"]):
        totals["total"] = totals["input"] + totals["output"]
    return {key: value for key, value in totals.items() if value}

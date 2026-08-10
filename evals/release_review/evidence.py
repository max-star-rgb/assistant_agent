from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.runtime.state import AgentState


class ReleaseToolCallEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    input: dict[str, Any]
    call_index: int = Field(ge=1)
    status: Literal["running", "succeeded", "failed"]
    before_final_response: bool


class ReleaseRunEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    calls: tuple[ReleaseToolCallEvidence, ...] = ()
    final_state: dict[str, Any] = Field(default_factory=dict)
    infrastructure_error: str | None = None

    @classmethod
    def from_state(
        cls,
        state: AgentState,
        events: Iterable[Any],
    ) -> "ReleaseRunEvidence":
        event_list = list(events)
        terminal_indices: dict[str, list[int]] = defaultdict(list)
        final_indices: list[int] = []
        for index, event in enumerate(event_list):
            name = _event_value(event, "canonical_event") or _event_value(event, "type")
            tool_name = _event_value(event, "tool_name")
            if name in {"tool.finished", "tool.failed", "tool_finished", "tool_failed"}:
                if isinstance(tool_name, str):
                    terminal_indices[tool_name].append(index)
            if name in {
                "response.delivered",
                "run.completed",
                "final_response",
                "agent_response",
            }:
                final_indices.append(index)

        final_index = min(final_indices) if final_indices else None
        offsets: dict[str, int] = defaultdict(int)
        calls: list[ReleaseToolCallEvidence] = []
        for index, call in enumerate(state.tool_calls, start=1):
            offset = offsets[call.tool_name]
            offsets[call.tool_name] += 1
            tool_terminals = terminal_indices.get(call.tool_name, [])
            terminal_index = tool_terminals[offset] if offset < len(tool_terminals) else None
            if terminal_index is not None and final_index is not None:
                before_final = terminal_index < final_index
            else:
                before_final = state.response is not None and call.status in {
                    "succeeded",
                    "failed",
                }
            calls.append(
                ReleaseToolCallEvidence(
                    tool_name=call.tool_name,
                    input=call.input,
                    call_index=index,
                    status=call.status,
                    before_final_response=before_final,
                )
            )

        return cls(
            calls=tuple(calls),
            final_state={
                "status": state.status,
                "plan_status": state.plan_status,
                "current_step_id": state.current_step_id,
                "response": (
                    state.response.model_dump(mode="json")
                    if state.response is not None
                    else None
                ),
                "tool_results": [
                    result.model_dump(mode="json") for result in state.tool_results
                ],
                "errors": [error.model_dump(mode="json") for error in state.errors],
            },
        )


def _event_value(event: Any, name: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


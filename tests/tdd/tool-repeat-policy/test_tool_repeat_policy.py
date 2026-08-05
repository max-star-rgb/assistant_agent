from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from pydantic import BaseModel

from assistant_agent.context.service import AssistantDecisionContext
from assistant_agent.runtime.assistant_loop_nodes import (
    AssistantLoopState,
    _apply_decision_guards,
    _execute_single_requested_tool_node,
    execute_requested_tool_node,
)
from assistant_agent.runtime.loop_guard import LoopGuard
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.base import ToolBase
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.shopping.tool import ShoppingSearchTool
from assistant_agent.tools.registry import ToolRegistry


class _ProbeInput(BaseModel):
    query: str


class _ProbeOutput(BaseModel):
    value: str


class _DefaultProbeTool(ToolBase):
    name = "default_probe"
    description = "Default repeat policy probe."
    input_schema = _ProbeInput
    output_schema = _ProbeOutput
    category = "read"

    def _run(self, input: _ProbeInput, context: object) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.query},
        )


class _RepeatableProbeTool(_DefaultProbeTool):
    name = "repeatable_probe"
    repeat_policy = "distinct_inputs"


def test_registry_projects_default_and_explicit_repeat_policy() -> None:
    """Catches Registry dropping a Tool's repeat policy declaration."""

    registry = ToolRegistry()
    registry.register(_DefaultProbeTool())
    registry.register(_RepeatableProbeTool())

    assert registry.get_spec("default_probe").repeat_policy == "once_per_run"
    assert registry.get_spec("repeatable_probe").repeat_policy == "distinct_inputs"


def _successful_state(tool_name: str, tool_input: dict[str, object]) -> AgentState:
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="continue",
    )
    state = AgentState.from_request(request)
    record = state.add_tool_call(tool_name, tool_input)
    state.complete_tool_call(
        record.tool_call_id,
        ToolResult(tool_name=tool_name, success=True, data={"value": "ok"}),
    )
    LoopGuard(state.request.metadata).record_complete_tool_success(
        tool_name=tool_name,
        tool_input=tool_input,
    )
    return state


def _guarded_decision(
    state: AgentState,
    registry: ToolRegistry,
    *,
    tool_name: str,
    tool_input: dict[str, object],
) -> AssistantToolCall:
    graph_state = cast(
        AssistantLoopState,
        {
            "request": state.request,
            "state": state,
            "tool_executor": SimpleNamespace(registry=registry),
            "outputs_by_step": {},
            "current_step_index": 0,
        },
    )
    return cast(
        AssistantToolCall,
        _apply_decision_guards(
            graph_state,
            AssistantToolCall(tool_name=tool_name, tool_input=tool_input),
            cast(AssistantDecisionContext, None),
        ),
    )


def test_default_tool_blocks_a_second_successful_call_with_different_input() -> None:
    """Catches the default policy accidentally allowing unbounded distinct calls."""

    registry = ToolRegistry()
    registry.register(_DefaultProbeTool())
    state = _successful_state("default_probe", {"query": "first"})

    decision = _guarded_decision(
        state,
        registry,
        tool_name="default_probe",
        tool_input={"query": "second"},
    )

    assert "tool_repeat_limit_reached" in decision.safety_notes


def test_repeatable_tool_allows_a_second_call_with_different_input() -> None:
    """Catches distinct-input opt-in being ignored by the Runtime guard."""

    registry = ToolRegistry()
    registry.register(_RepeatableProbeTool())
    state = _successful_state("repeatable_probe", {"query": "first"})

    decision = _guarded_decision(
        state,
        registry,
        tool_name="repeatable_probe",
        tool_input={"query": "second"},
    )

    assert "tool_repeat_limit_reached" not in decision.safety_notes
    assert "duplicate_complete_tool_call" not in decision.safety_notes


def test_repeatable_tool_still_reuses_an_identical_complete_call() -> None:
    """Catches repeat opt-in bypassing the existing same-input deduplication guard."""

    registry = ToolRegistry()
    registry.register(_RepeatableProbeTool())
    state = _successful_state("repeatable_probe", {"query": "same"})

    decision = _guarded_decision(
        state,
        registry,
        tool_name="repeatable_probe",
        tool_input={"query": "same"},
    )

    assert "duplicate_complete_tool_call" in decision.safety_notes


def test_shopping_search_opts_into_distinct_input_repeats() -> None:
    """Catches reintroducing the shopping-specific once-per-run hard code."""

    registry = ToolRegistry()
    registry.register(
        ShoppingSearchTool(
            search_adapter=cast(Any, object()),
            compare_adapter=cast(Any, object()),
        )
    )
    state = _successful_state(
        "shopping_search",
        {"needs": [{"keyword": "蓝牙耳机"}]},
    )

    decision = _guarded_decision(
        state,
        registry,
        tool_name="shopping_search",
        tool_input={"needs": [{"keyword": "降噪蓝牙耳机"}]},
    )

    assert "tool_repeat_limit_reached" not in decision.safety_notes
    assert "run_tool_call_limit_reached" not in decision.safety_notes


def test_execution_boundary_blocks_unguarded_second_default_call() -> None:
    """Catches a later native batch call bypassing the decision-level repeat guard."""

    registry = ToolRegistry()
    registry.register(_DefaultProbeTool())
    state = _successful_state("default_probe", {"query": "first"})
    decision = AssistantToolCall(
        tool_name="default_probe",
        tool_input={"query": "second"},
    )

    executed = _execute_single_requested_tool_node(
        cast(
            AssistantLoopState,
            {
                "request": state.request,
                "state": state,
                "tool_executor": ToolExecutor(registry=registry),
                "outputs_by_step": {},
                "current_step_index": 0,
                "assistant_output": decision,
                "tool_observations": [],
            },
        )
    )

    assert len(state.tool_calls) == 1
    assert executed["tool_observations"][-1]["error"]["code"] == (
        "tool_repeat_limit_reached"
    )
    assert executed["run_phase"].value == "finalize"


def test_repeatable_tool_still_obeys_the_global_tool_call_budget() -> None:
    """Catches distinct-input opt-in bypassing max_tool_iterations."""

    registry = ToolRegistry()
    registry.register(_RepeatableProbeTool())
    state = AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="search twice",
        )
    )

    executed = execute_requested_tool_node(
        cast(
            AssistantLoopState,
            {
                "request": state.request,
                "state": state,
                "tool_executor": ToolExecutor(registry=registry),
                "outputs_by_step": {},
                "current_step_index": 0,
                "pending_tool_calls": [
                    AssistantToolCall(
                        tool_name="repeatable_probe",
                        tool_input={"query": "first"},
                    ),
                    AssistantToolCall(
                        tool_name="repeatable_probe",
                        tool_input={"query": "second"},
                    ),
                ],
                "tool_observations": [],
                "max_tool_iterations": 1,
            },
        )
    )

    assert len(state.tool_calls) == 1
    assert executed["tool_calls_used"] == 1
    assert state.request.metadata["tool_calls_skipped_for_budget"] == 1
    assert executed["run_phase"].value == "finalize"

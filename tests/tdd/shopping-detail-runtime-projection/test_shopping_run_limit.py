from __future__ import annotations

from typing import cast

from assistant_agent.context.service import AssistantDecisionContext
from assistant_agent.runtime.assistant_loop_nodes import (
    AssistantLoopState,
    _apply_decision_guards,
    _execute_single_requested_tool_node,
)
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState


def _guarded_decision(
    state: AgentState,
    *,
    tool_name: str,
    tool_input: dict[str, object],
) -> AssistantToolCall:
    graph_state = cast(
        AssistantLoopState,
        {
            "request": state.request,
            "state": state,
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


def test_second_shopping_call_is_blocked_even_when_arguments_change() -> None:
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="先搜手机，再换一个关键词搜",
    )
    state = AgentState.from_request(request)
    state.add_tool_call("shopping_search", {"needs": [{"keyword": "小米14"}]})

    decision = _guarded_decision(
        state,
        tool_name="shopping_search",
        tool_input={"needs": [{"keyword": "小米15"}]},
    )

    assert "run_tool_call_limit_reached" in decision.safety_notes

    executed = _execute_single_requested_tool_node(
        cast(
            AssistantLoopState,
            {
                "request": request,
                "state": state,
                "tool_executor": object(),
                "outputs_by_step": {},
                "current_step_index": 0,
                "assistant_output": decision,
                "tool_observations": [],
            },
        )
    )

    assert len(state.tool_calls) == 1
    assert executed["tool_observations"][-1]["error"]["code"] == (
        "run_tool_call_limit_reached"
    )


def test_new_run_can_call_shopping_again() -> None:
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="新一轮再搜手机",
    )
    state = AgentState.from_request(request)

    decision = _guarded_decision(
        state,
        tool_name="shopping_search",
        tool_input={"needs": [{"keyword": "小米15"}]},
    )

    assert "run_tool_call_limit_reached" not in decision.safety_notes

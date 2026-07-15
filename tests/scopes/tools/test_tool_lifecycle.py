from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.tool_call_boundary import build_post_tool_call_summary


def _state() -> AgentState:
    return AgentState.from_request(
        UserRequest(user_id="u1", session_id="s1", text="hello"),
        run_id="run-1",
    )


def test_post_boundary_marks_pending_confirmation_lifecycle() -> None:
    post = build_post_tool_call_summary(
        tool_name="memory_save",
        result=ToolResult(
            tool_name="memory_save",
            success=True,
            data={"requires_confirmation": True, "confirmation_id": "confirm-1"},
        ),
        state=_state(),
    )

    assert post["lifecycle"] == {
        "status": "pending_confirmation",
        "committed": False,
        "cancellable": True,
        "next_action": "await_confirmation",
    }


def test_post_boundary_marks_interrupted_after_commit_lifecycle() -> None:
    post = build_post_tool_call_summary(
        tool_name="image_generation",
        result=ToolResult(
            tool_name="image_generation",
            success=True,
            data={"summary": "created image"},
            output_ref="mock://image/1",
        ),
        state=_state(),
        cancel_metadata={
            "cancel_source": "interrupt",
            "cancel_reason": "barge_in_after_side_effect",
        },
    )

    assert post["lifecycle"]["status"] == "interrupted_after_commit"
    assert post["lifecycle"]["committed"] is True
    assert post["lifecycle"]["cancellable"] is False
    assert post["lifecycle"]["next_action"] == "report_committed"


def test_post_boundary_marks_cancelled_before_commit_lifecycle() -> None:
    post = build_post_tool_call_summary(
        tool_name="web_search",
        result=ToolResult(
            tool_name="web_search",
            success=False,
            data={"cancelled": True},
            error="cancelled",
        ),
        state=_state(),
        cancel_metadata={
            "cancel_source": "deadline",
            "cancel_reason": "run_deadline_expired",
        },
    )

    assert post["lifecycle"]["status"] == "cancelled_before_commit"
    assert post["lifecycle"]["committed"] is False
    assert post["lifecycle"]["cancellable"] is False
    assert post["lifecycle"]["next_action"] == "restart_or_skip"

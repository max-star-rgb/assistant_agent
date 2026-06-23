from pydantic import BaseModel

from multimodal_agent.agent.workflow import AgentWorkflow
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.run_history import RunHistoryStore
from multimodal_agent.services.tool_history import ToolHistoryStore
from multimodal_agent.tools.base import MockTool, ToolContext
from multimodal_agent.tools.registry import ToolRegistry


def test_history_records_successful_agent_run_and_tool_calls(tmp_path) -> None:
    run_history = RunHistoryStore(tmp_path / "runs.jsonl")
    tool_history = ToolHistoryStore(tmp_path / "tool_calls.jsonl")

    state = AgentWorkflow(run_history=run_history, tool_history=tool_history).run(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我找视频里的鞋子并比价",
            video_ids=["v1"],
        )
    )

    run_records = run_history.read_all()
    tool_records = tool_history.read_all()

    assert state.run_id
    assert [record.status for record in run_records] == ["started", "completed"]
    assert run_records[-1].run_id == state.run_id
    assert run_records[-1].intent == "multi_step_orchestration"
    assert run_records[-1].latency_ms is not None

    completed_tools = [record for record in tool_records if record.status == "succeeded"]
    assert completed_tools
    assert all(record.run_id == state.run_id for record in completed_tools)
    assert all(record.user_id == "u1" for record in completed_tools)
    assert all(record.session_id == "s1" for record in completed_tools)
    assert all(record.call_id for record in completed_tools)
    assert all(record.tool_name for record in completed_tools)
    assert all(record.latency_ms is not None for record in completed_tools)


def test_history_records_failed_tool_call(tmp_path) -> None:
    class FailingInput(BaseModel):
        query: str | None = None

    class FailingTool(MockTool):
        name = "product_search"
        description = "Always fails for history tests."
        input_schema = FailingInput
        output_schema = FailingInput

        def _run(self, input: FailingInput, context: ToolContext) -> ToolResult:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="mock failure",
                latency_ms=7,
            )

    registry = ToolRegistry()
    registry.register(FailingTool())
    run_history = RunHistoryStore(tmp_path / "runs.jsonl")
    tool_history = ToolHistoryStore(tmp_path / "tool_calls.jsonl")

    state = AgentWorkflow(
        registry=registry,
        run_history=run_history,
        tool_history=tool_history,
    ).run(UserRequest(user_id="u1", session_id="s1", text="找相似款"))

    tool_records = tool_history.read_all()
    run_records = run_history.read_all()

    assert state.status == "failed"
    assert any(record.status == "failed" for record in tool_records)
    failed_record = [record for record in tool_records if record.status == "failed"][0]
    assert failed_record.call_id
    assert failed_record.tool_name == "product_search"
    assert failed_record.error == "mock failure"
    assert failed_record.latency_ms == 7
    assert run_records[-1].status == "failed"
    assert run_records[-1].error == "mock failure"


def test_history_can_delete_records_by_user(tmp_path) -> None:
    run_history = RunHistoryStore(tmp_path / "runs.jsonl")
    tool_history = ToolHistoryStore(tmp_path / "tool_calls.jsonl")
    AgentWorkflow(run_history=run_history, tool_history=tool_history).run(
        UserRequest(user_id="u1", session_id="s1", text="帮我找相似款")
    )
    AgentWorkflow(run_history=run_history, tool_history=tool_history).run(
        UserRequest(user_id="u2", session_id="s1", text="帮我找相似款")
    )

    assert run_history.delete_by_user("u1") >= 1
    assert tool_history.delete_by_user("u1") >= 1
    assert run_history.list_by_user("u1") == []
    assert tool_history.list_by_user("u1") == []
    assert run_history.list_by_user("u2")
    assert tool_history.list_by_user("u2")

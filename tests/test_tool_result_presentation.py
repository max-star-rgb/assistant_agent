from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.realtime_task_state import (
    InMemoryRealtimeTaskStateStore,
    prepare_realtime_task_state_request,
    record_realtime_task_state_run_artifacts,
)
from assistant_agent.services.tool_call_boundary import build_post_tool_call_summary
from assistant_agent.services.tool_history import ToolHistoryStore
from assistant_agent.schemas.tool_observation import observation_from_tool_result


def test_tool_result_model_observation_is_assistant_facing_data() -> None:
    result = ToolResult(
        tool_name="product_search",
        success=True,
        data={
            "summary": "legacy summary with token sk-test",
            "items": [{"title": "legacy item"}],
        },
        model_observation={
            "summary": "Found one safe product.",
            "items": [{"title": "safe item"}],
        },
        raw_data_ref="provider://raw/search-1",
    )

    observation = observation_from_tool_result(result)

    assert observation.summary == "Found one safe product."
    assert observation.structured_output == {
        "summary": "Found one safe product.",
        "items": [{"title": "safe item"}],
    }
    assert "legacy item" not in str(observation)
    assert "provider://raw/search-1" not in str(observation)


def test_post_boundary_uses_trace_summary_without_raw_data_ref() -> None:
    state = AgentState.from_request(
        UserRequest(user_id="u1", session_id="s1", text="hello"),
        run_id="run-1",
    )
    result = ToolResult(
        tool_name="web_search",
        success=True,
        data={"summary": "legacy summary", "raw_provider_payload": {"token": "sk-test"}},
        trace_summary={"summary": "Trace-safe web search.", "result_count": 2},
        raw_data_ref="provider://raw/web-1",
    )

    post = build_post_tool_call_summary(
        tool_name="web_search",
        result=result,
        state=state,
    )

    assert post["observation_summary"] == {
        "success": True,
        "summary": "Trace-safe web search.",
        "output_ref": None,
        "result_count": 2,
    }
    assert "sk-test" not in str(post)
    assert "provider://raw/web-1" not in str(post)


def test_realtime_task_state_uses_voice_summary_for_resume_snapshot() -> None:
    class State:
        request = UserRequest(
            user_id="u1",
            session_id="s1",
            text="记住我通勤时喜欢降噪耳机",
            metadata={"enable_realtime_task_state": True},
        )
        status = "completed"
        run_id = "run-1"
        tool_results = [
            ToolResult(
                tool_name="memory_save",
                success=True,
                data={"summary": "Saved sensitive preference token sk-test."},
                voice_summary="已保存你的通勤降噪偏好。",
                output_ref="memory://preference/noise",
            )
        ]

    store = InMemoryRealtimeTaskStateStore()
    prepared = prepare_realtime_task_state_request(State.request, store=store)
    record_realtime_task_state_run_artifacts(State, store=store)
    interrupted = prepare_realtime_task_state_request(
        prepared.model_copy(
            update={
                "text": "等等，改成优先轻便。",
                "metadata": {
                    "enable_realtime_task_state": True,
                    "control": "interrupt",
                },
            },
            deep=True,
        ),
        store=store,
    )

    snapshot = interrupted.metadata["realtime_task_state"]

    assert snapshot["committed_side_effect_count"] == 1
    assert snapshot["side_effects"][0]["summary"] == "已保存你的通勤降噪偏好。"
    assert "sk-test" not in str(snapshot)


def test_tool_history_separates_prompt_summary_and_audit_payload(tmp_path) -> None:
    store = ToolHistoryStore(tmp_path / "tool_calls.jsonl")

    store.record_end(
        "run-1",
        "call-1",
        "web_search",
        "succeeded",
        12,
        output_summary={"success": True, "summary": "Prompt-safe summary."},
        audit_payload={"request_id": "audit-1", "redacted": True},
        raw_data_ref="provider://raw/web-1",
    )

    record = store.read_all()[0]

    assert record.output_summary == {"success": True, "summary": "Prompt-safe summary."}
    assert record.audit_payload == {"request_id": "audit-1", "redacted": True}
    assert record.raw_data_ref == "provider://raw/web-1"
    assert "provider://raw/web-1" not in str(record.output_summary)

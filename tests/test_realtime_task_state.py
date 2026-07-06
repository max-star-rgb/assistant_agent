from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.assistant_run_service import (
    InMemoryConversationStore,
    run_assistant_request,
)
from assistant_agent.services.context.builder import build_assistant_context_pack
from assistant_agent.services.context.renderer import render_prompt_json_context
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.realtime_task_state import (
    REALTIME_TASK_STATE_METADATA_KEY,
    InMemoryRealtimeTaskStateStore,
    prepare_realtime_task_state_request,
)


def test_realtime_task_state_skips_non_realtime_request_by_default() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="普通 HTTP 请求")

    prepared = prepare_realtime_task_state_request(
        request,
        store=InMemoryRealtimeTaskStateStore(),
    )

    assert prepared.metadata == {}


def test_realtime_task_state_first_turn_creates_snapshot() -> None:
    store = InMemoryRealtimeTaskStateStore()
    request = _realtime_request(
        "帮我比较三款 500 元以内的蓝牙耳机",
        run_id="run-1",
        turn_id="turn-1",
    )

    prepared = prepare_realtime_task_state_request(request, store=store)

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    assert snapshot["schema_version"] == "realtime_task_state_v1"
    assert snapshot["status"] == "active"
    assert snapshot["objective"] == "帮我比较三款 500 元以内的蓝牙耳机"
    assert snapshot["current_user_text"] == "帮我比较三款 500 元以内的蓝牙耳机"
    assert snapshot["source_turn_ids"] == ["turn-1"]
    assert snapshot["source_run_ids"] == ["run-1"]
    assert snapshot["revision_count"] == 0
    assert "realtime_task_state_text" in prepared.metadata


def test_realtime_task_state_queued_followup_keeps_original_objective() -> None:
    store = InMemoryRealtimeTaskStateStore()
    prepare_realtime_task_state_request(
        _realtime_request("帮我比较三款 500 元以内的蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        store=store,
    )

    prepared = prepare_realtime_task_state_request(
        _realtime_request("再补充看看续航", run_id="run-2", turn_id="turn-2"),
        store=store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    assert snapshot["status"] == "active"
    assert snapshot["objective"] == "帮我比较三款 500 元以内的蓝牙耳机"
    assert snapshot["current_user_text"] == "再补充看看续航"
    assert snapshot["revision_count"] == 0
    assert snapshot["source_turn_ids"] == ["turn-1", "turn-2"]
    assert snapshot["source_run_ids"] == ["run-1", "run-2"]


def test_realtime_task_state_interrupt_creates_intent_revision() -> None:
    store = InMemoryRealtimeTaskStateStore()
    prepare_realtime_task_state_request(
        _realtime_request("帮我比较三款 500 元以内的蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        store=store,
    )

    prepared = prepare_realtime_task_state_request(
        _realtime_request(
            "等等，优先考虑降噪和通勤佩戴舒适度",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        store=store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    assert snapshot["status"] == "revising"
    assert snapshot["objective"] == "帮我比较三款 500 元以内的蓝牙耳机"
    assert snapshot["constraints"] == ["等等，优先考虑降噪和通勤佩戴舒适度"]
    assert snapshot["latest_revision"]["user_text"] == "等等，优先考虑降噪和通勤佩戴舒适度"
    assert snapshot["latest_revision"]["strategy"] == "restart"
    assert snapshot["latest_revision"]["revision_type"] == "add_constraint"
    assert snapshot["revision_count"] == 1


def test_run_assistant_request_injects_task_state_before_runtime() -> None:
    task_store = InMemoryRealtimeTaskStateStore()
    conversation_store = InMemoryConversationStore()
    runtime = _RecordingRuntime()

    run_assistant_request(
        _realtime_request("帮我比较三款 500 元以内的蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        runtime=runtime,
        conversation_store=conversation_store,
        realtime_task_state_store=task_store,
        load_env=False,
    )
    run_assistant_request(
        _realtime_request(
            "等等，优先考虑降噪和通勤佩戴舒适度",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        runtime=runtime,
        conversation_store=conversation_store,
        realtime_task_state_store=task_store,
        load_env=False,
    )

    latest_request = runtime.requests[-1]
    snapshot = latest_request.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    assert snapshot["objective"] == "帮我比较三款 500 元以内的蓝牙耳机"
    assert snapshot["latest_revision"]["user_text"] == "等等，优先考虑降噪和通勤佩戴舒适度"
    assert latest_request.metadata["conversation_turn_index"] == 2


def test_realtime_task_state_reuses_completed_product_search_artifact_on_interrupt() -> None:
    task_store = InMemoryRealtimeTaskStateStore()
    runtime = _ProductSearchRuntime()

    run_assistant_request(
        _realtime_request("帮我比较三款 500 元以内的蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        runtime=runtime,
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )
    prepared = prepare_realtime_task_state_request(
        _realtime_request(
            "等等，优先考虑降噪和通勤佩戴舒适度",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        store=task_store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    reusable_artifact = snapshot["reusable_artifacts"][0]
    assert snapshot["latest_revision"]["strategy"] == "reuse_and_replan"
    assert snapshot["continuation_strategy"] == "reuse_and_replan"
    assert snapshot["stale_artifact_count"] == 0
    assert snapshot["pending_confirmation_count"] == 0
    assert snapshot["committed_side_effect_count"] == 0
    assert snapshot["side_effects"][0]["tool_name"] == "product_search"
    assert snapshot["side_effects"][0]["effect_level"] == "external_read"
    assert reusable_artifact["tool_name"] == "product_search"
    assert reusable_artifact["output_ref"] == "mock://products/headphones"
    assert reusable_artifact["context"]["structured_output"]["items"][0]["title"] == "通勤降噪耳机 A"
    assert "raw_provider_payload" not in str(reusable_artifact)


def test_realtime_task_state_marks_artifacts_stale_when_user_restarts_search() -> None:
    task_store = InMemoryRealtimeTaskStateStore()
    runtime = _ProductSearchRuntime()

    run_assistant_request(
        _realtime_request("帮我比较三款 500 元以内的蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        runtime=runtime,
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )
    prepared = prepare_realtime_task_state_request(
        _realtime_request(
            "重新搜索，换一批，不要之前的商品结果",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        store=task_store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    state = AgentState.from_request(prepared)
    pack = build_assistant_context_pack(
        state=state,
        observations=[],
        tool_specs=[],
        iteration=0,
        max_iterations=5,
    )
    prompt = render_prompt_json_context(pack).prompt_json or ""

    assert snapshot["latest_revision"]["strategy"] == "restart"
    assert snapshot["reusable_artifacts"] == []
    assert snapshot["stale_artifact_count"] == 1
    assert "通勤降噪耳机 A" not in prompt


def test_run_assistant_request_emits_strategy_progress_for_reused_artifacts() -> None:
    task_store = InMemoryRealtimeTaskStateStore()
    conversation_store = InMemoryConversationStore()
    runtime = _ProductSearchRuntime()

    run_assistant_request(
        _realtime_request("帮我比较三款 500 元以内的蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        runtime=runtime,
        conversation_store=conversation_store,
        realtime_task_state_store=task_store,
        load_env=False,
    )
    sink = ListEventSink()
    artifacts = run_assistant_request(
        _realtime_request(
            "等等，优先考虑降噪和通勤佩戴舒适度",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        runtime=_RecordingRuntime(),
        event_sink=sink,
        conversation_store=conversation_store,
        realtime_task_state_store=task_store,
        load_env=False,
    )

    progress_events = [event for event in artifacts.events if event.type == "tool_progress"]
    assert progress_events
    progress = progress_events[0]
    assert progress.tool_name == "task_state"
    assert progress.text == "Using previous findings to revise the task."
    assert progress.payload["stage"] == "task_state"
    assert progress.payload["strategy"] == "reuse_and_replan"
    assert progress.payload["reusable_artifact_count"] == 1


def test_realtime_task_state_asks_confirmation_after_pending_side_effect() -> None:
    task_store = InMemoryRealtimeTaskStateStore()

    run_assistant_request(
        _realtime_request("请记住我的项目路径是 /home/alice/private/project", run_id="run-1", turn_id="turn-1"),
        runtime=_PendingConfirmationRuntime(),
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )
    prepared = prepare_realtime_task_state_request(
        _realtime_request(
            "等等，先别保存这个",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        store=task_store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    side_effect = snapshot["side_effects"][0]
    assert snapshot["latest_revision"]["strategy"] == "ask_confirmation"
    assert snapshot["pending_confirmation_count"] == 1
    assert snapshot["committed_side_effect_count"] == 0
    assert side_effect["tool_name"] == "memory_save"
    assert side_effect["effect_level"] == "pending_confirmation"
    assert side_effect["requires_confirmation"] is True
    assert side_effect["confirmation_id"] == "memory_confirmation_1"


def test_realtime_task_state_reports_committed_side_effect_on_interrupt() -> None:
    task_store = InMemoryRealtimeTaskStateStore()

    run_assistant_request(
        _realtime_request("请记住我通勤时优先降噪", run_id="run-1", turn_id="turn-1"),
        runtime=_CommittedMemoryRuntime(),
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )
    sink = ListEventSink()
    artifacts = run_assistant_request(
        _realtime_request(
            "等等，不要保存这个偏好",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        runtime=_RecordingRuntime(),
        event_sink=sink,
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )

    snapshot = artifacts.state.request.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    progress = [event for event in artifacts.events if event.type == "tool_progress"][0]
    side_effect = snapshot["side_effects"][0]
    assert snapshot["latest_revision"]["strategy"] == "report_committed"
    assert snapshot["committed_side_effect_count"] == 1
    assert side_effect["tool_name"] == "memory_save"
    assert side_effect["effect_level"] == "committed"
    assert side_effect["requires_confirmation"] is False
    assert side_effect["output_ref"] == "memory://preference/noise"
    assert progress.payload["strategy"] == "report_committed"
    assert progress.text == "Action already committed; preparing a safe follow-up."


def test_realtime_task_state_compensates_created_artifact_on_interrupt() -> None:
    task_store = InMemoryRealtimeTaskStateStore()

    run_assistant_request(
        _realtime_request("生成一张蓝牙耳机海报", run_id="run-1", turn_id="turn-1"),
        runtime=_ImageGenerationRuntime(),
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )
    prepared = prepare_realtime_task_state_request(
        _realtime_request(
            "等等，改成通勤场景",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        store=task_store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    side_effect = snapshot["side_effects"][0]
    assert snapshot["latest_revision"]["strategy"] == "compensate"
    assert snapshot["compensatable_side_effect_count"] == 1
    assert side_effect["tool_name"] == "image_generation"
    assert side_effect["effect_level"] == "compensatable"
    assert "replacement" in side_effect["compensation_hint"]


def test_realtime_task_state_treats_unknown_successful_tool_as_committed() -> None:
    task_store = InMemoryRealtimeTaskStateStore()

    run_assistant_request(
        _realtime_request("发送通知给团队", run_id="run-1", turn_id="turn-1"),
        runtime=_UnknownSuccessfulToolRuntime(),
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )
    prepared = prepare_realtime_task_state_request(
        _realtime_request(
            "等等，不要发",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        store=task_store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    assert snapshot["latest_revision"]["strategy"] == "report_committed"
    assert snapshot["committed_side_effect_count"] == 1
    assert snapshot["side_effects"][0]["tool_name"] == "custom_notification"


class _RecordingRuntime:
    def __init__(self) -> None:
        self.requests: list[UserRequest] = []

    def run_state(self, request: UserRequest, **kwargs) -> AgentState:
        self.requests.append(request)
        state = AgentState.from_request(request)
        state.set_response(AgentResponse(message=f"ok: {request.text}"))
        return state


class _ProductSearchRuntime:
    def run_state(self, request: UserRequest, **kwargs) -> AgentState:
        state = AgentState.from_request(request)
        state.tool_results.append(
            ToolResult(
                tool_name="product_search",
                success=True,
                output_ref="mock://products/headphones",
                data={
                    "provider": "mock",
                    "query_used": "蓝牙耳机 500 元以内",
                    "total": 3,
                    "items": [
                        {
                            "product_id": "p1",
                            "title": "通勤降噪耳机 A",
                            "price": 399,
                            "currency": "CNY",
                            "product_url": "https://example.test/p1",
                            "url_status": "verified",
                            "raw_html": "<html>secret payload</html>",
                        }
                    ],
                    "raw_provider_payload": {"token": "sk-test"},
                },
            )
        )
        state.set_response(AgentResponse(message="找到三款耳机。"))
        return state


class _PendingConfirmationRuntime:
    def run_state(self, request: UserRequest, **kwargs) -> AgentState:
        state = AgentState.from_request(request)
        state.tool_results.append(
            ToolResult(
                tool_name="memory_save",
                success=False,
                data={
                    "requires_confirmation": True,
                    "confirmation_id": "memory_confirmation_1",
                    "summary": "Sensitive memory write needs user confirmation.",
                },
                error="memory_confirmation_required: user confirmation is required",
            )
        )
        state.set_response(AgentResponse(message="需要确认后才能保存。"))
        return state


class _CommittedMemoryRuntime:
    def run_state(self, request: UserRequest, **kwargs) -> AgentState:
        state = AgentState.from_request(request)
        state.tool_results.append(
            ToolResult(
                tool_name="memory_save",
                success=True,
                output_ref="memory://preference/noise",
                data={"summary": "Saved commuting noise-cancellation preference."},
            )
        )
        state.set_response(AgentResponse(message="已保存偏好。"))
        return state


class _ImageGenerationRuntime:
    def run_state(self, request: UserRequest, **kwargs) -> AgentState:
        state = AgentState.from_request(request)
        state.tool_results.append(
            ToolResult(
                tool_name="image_generation",
                success=True,
                output_ref="image://poster-1",
                data={"summary": "Generated a headphone poster artifact."},
            )
        )
        state.set_response(AgentResponse(message="已生成海报。"))
        return state


class _UnknownSuccessfulToolRuntime:
    def run_state(self, request: UserRequest, **kwargs) -> AgentState:
        state = AgentState.from_request(request)
        state.tool_results.append(
            ToolResult(
                tool_name="custom_notification",
                success=True,
                output_ref="notification://team-1",
                data={"summary": "Notification was sent to the team."},
            )
        )
        state.set_response(AgentResponse(message="已发送通知。"))
        return state


def _realtime_request(
    text: str,
    *,
    run_id: str,
    turn_id: str,
    control: str | None = None,
) -> UserRequest:
    metadata = {
        "source": "realtime_agent_backend",
        "realtime": {"run_id": run_id, "turn_id": turn_id},
    }
    if control:
        metadata["control"] = control
    return UserRequest(user_id="u1", session_id="s1", text=text, metadata=metadata)

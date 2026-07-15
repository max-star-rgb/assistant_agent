from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.realtime_turn_arbitration import (
    REALTIME_TURN_ARBITRATION_METADATA_KEY,
    RealtimeTurnArbitrationRequest,
    normalize_arbitration_decision,
)
from assistant_agent.schemas.tools import ToolExecutionPolicy, ToolResult
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
    RealtimeTaskState,
    SideEffectRecord,
    TaskArtifact,
    apply_cancel_only_arbitration_to_task_state,
    format_realtime_task_state_snapshot,
    prepare_realtime_task_state_request,
    reduce_realtime_task_state_event,
    snapshot_from_task_state,
)


def test_realtime_task_state_skips_non_realtime_request_by_default() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="普通 HTTP 请求")

    prepared = prepare_realtime_task_state_request(
        request,
        store=InMemoryRealtimeTaskStateStore(),
    )

    assert prepared.metadata == {}


def test_realtime_task_state_skips_plain_gateway_metadata_without_realtime_capability() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="普通 Gateway chat facade 请求",
        metadata={
            "gateway": {
                "history": ["普通 Gateway chat facade 请求"],
                "entry_capabilities": {
                    "supports_interrupt": True,
                    "supports_realtime_task_state": False,
                },
            },
            "realtime": {"run_id": "run-1", "turn_id": "turn-1"},
        },
    )

    prepared = prepare_realtime_task_state_request(
        request,
        store=InMemoryRealtimeTaskStateStore(),
    )

    assert prepared.metadata == request.metadata


def test_realtime_task_state_enables_for_explicit_interaction_mode() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="实时通话请求",
        metadata={
            "interaction_mode": "realtime",
            "realtime": {"run_id": "run-1", "turn_id": "turn-1"},
        },
    )

    prepared = prepare_realtime_task_state_request(
        request,
        store=InMemoryRealtimeTaskStateStore(),
    )

    assert prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]["objective"] == "实时通话请求"
    assert prepared.metadata["realtime_task_state_enabled"] is True


def test_realtime_task_state_enables_for_entry_capability() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="媒体入口请求",
        metadata={
            "gateway": {
                "entry_capabilities": {
                    "supports_interrupt": True,
                    "supports_realtime_task_state": True,
                },
            },
            "realtime": {"run_id": "run-1", "turn_id": "turn-1"},
        },
    )

    prepared = prepare_realtime_task_state_request(
        request,
        store=InMemoryRealtimeTaskStateStore(),
    )

    assert prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]["objective"] == "媒体入口请求"


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


def test_semantic_revision_replaces_constraint_instead_of_appending() -> None:
    store = InMemoryRealtimeTaskStateStore()
    prepare_realtime_task_state_request(
        _realtime_request("帮我挑选通勤耳机", run_id="run-1", turn_id="turn-1"),
        store=store,
    )
    prepare_realtime_task_state_request(
        _realtime_request("预算 500 元以内", run_id="run-2", turn_id="turn-2", control="interrupt"),
        store=store,
    )

    prepared = prepare_realtime_task_state_request(
        _semantic_interrupt_request(
            "预算改成 1000 元以内",
            run_id="run-3",
            turn_id="turn-3",
            disposition="REVISE_ACTIVE",
            revision_type="replace_constraint",
        ),
        store=store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    assert snapshot["objective"] == "帮我挑选通勤耳机"
    assert snapshot["constraints"] == ["预算改成 1000 元以内"]
    assert snapshot["latest_revision"]["revision_type"] == "replace_constraint"
    assert snapshot["latest_revision"]["metadata"]["source"] == "semantic_llm"


def test_semantic_replace_changes_goal_stales_artifacts_and_preserves_side_effects() -> None:
    store = InMemoryRealtimeTaskStateStore()
    original = RealtimeTaskState(
        task_id="rtask:u1:s1",
        user_id="u1",
        session_id="s1",
        objective="查询北京周末天气",
        constraints=["只看室外活动"],
        artifacts=[
            TaskArtifact(
                artifact_id="artifact-weather",
                task_id="rtask:u1:s1",
                run_id="run-1",
                kind="observation",
                reuse_policy="reusable",
                summary="北京周末天气摘要",
            )
        ],
        side_effects=[
            SideEffectRecord(
                record_id="effect-reminder",
                task_id="rtask:u1:s1",
                run_id="run-1",
                tool_name="calendar_create_event",
                effect_level="committed",
                summary="已创建天气提醒",
            )
        ],
    )
    store.save(original)

    prepared = prepare_realtime_task_state_request(
        _semantic_interrupt_request(
            "改为设置明天上午九点的会议提醒",
            run_id="run-2",
            turn_id="turn-2",
            disposition="REPLACE_ACTIVE",
            revision_type="change_goal",
        ),
        store=store,
    )

    updated = store.get("u1", "s1")
    assert updated is not None
    assert updated.objective == "改为设置明天上午九点的会议提醒"
    assert updated.constraints == []
    assert updated.revisions[-1].revision_type == "change_goal"
    assert all(artifact.reuse_policy == "stale" for artifact in updated.artifacts)
    assert updated.side_effects == original.side_effects
    assert prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]["committed_side_effect_count"] == 1


def test_semantic_confirm_records_revision_without_turning_text_into_constraint() -> None:
    store = InMemoryRealtimeTaskStateStore()
    prepare_realtime_task_state_request(
        _realtime_request("帮我找三款耳机", run_id="run-1", turn_id="turn-1"),
        store=store,
    )

    prepared = prepare_realtime_task_state_request(
        _semantic_interrupt_request(
            "对，就按这个方向继续",
            run_id="run-2",
            turn_id="turn-2",
            disposition="REVISE_ACTIVE",
            revision_type="confirm",
        ),
        store=store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    assert snapshot["constraints"] == []
    assert snapshot["latest_revision"]["revision_type"] == "confirm"


def test_cancel_only_arbitration_marks_task_cancelled_without_dropping_side_effects() -> None:
    store = InMemoryRealtimeTaskStateStore()
    original = RealtimeTaskState(
        task_id="rtask:u1:s1",
        user_id="u1",
        session_id="s1",
        objective="查询北京天气",
        side_effects=[
            SideEffectRecord(
                record_id="effect-1",
                task_id="rtask:u1:s1",
                run_id="run-1",
                tool_name="calendar_create_event",
                effect_level="committed",
                summary="已创建天气提醒",
            )
        ],
    )
    store.save(original)
    request, decision = _arbitration(
        disposition="CANCEL_ONLY",
        revision_type="cancel_goal",
    )

    cancelled = apply_cancel_only_arbitration_to_task_state(
        user_id="u1",
        session_id="s1",
        turn_id="turn-2",
        run_id="run-2",
        user_text="先别查了",
        decision=decision,
        store=store,
    )

    assert cancelled.status == "cancelled"
    assert cancelled.tts_state == "interrupted"
    assert cancelled.revisions[-1].revision_type == "cancel_goal"
    assert cancelled.revisions[-1].metadata["decision_id"] == request.decision_id
    assert cancelled.side_effects == original.side_effects


def test_realtime_task_state_records_speech_turn_and_barge_in_source_on_interrupt() -> None:
    store = InMemoryRealtimeTaskStateStore()
    prepare_realtime_task_state_request(
        _realtime_request("帮我比较三款 500 元以内的蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        store=store,
    )

    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="等等，换成通勤降噪优先",
        audio_id="audio-turn-2",
        metadata={
            "source": "realtime_media_websocket",
            "interaction_mode": "realtime",
            "control": "interrupt",
            "realtime": {
                "run_id": "run-2",
                "turn_id": "turn-2",
                "speech_turn_id": "speech-turn-2",
            },
        },
    )

    prepared = prepare_realtime_task_state_request(request, store=store)

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    assert snapshot["status"] == "revising"
    assert snapshot["speech_turn_id"] == "speech-turn-2"
    assert snapshot["barge_in_source"] == "transcript"
    assert snapshot["tts_state"] == "interrupted"


def test_realtime_task_state_reducer_tracks_pending_tool_and_display_progress() -> None:
    state = RealtimeTaskState(task_id="rtask:u1:s1", user_id="u1", session_id="s1", objective="找耳机")

    state = reduce_realtime_task_state_event(
        state,
        event_type="tool.started",
        payload={
            "event_id": "evt-tool-start",
            "run_id": "run-1",
            "tool_name": "product_search",
            "current_step": "product_search",
        },
    )
    state = reduce_realtime_task_state_event(
        state,
        event_type="run.progress",
        text="I am on it.",
        payload={
            "event_id": "evt-progress-1",
            "source": "realtime_sla_fallback",
            "replaceable": True,
            "display_only": True,
            "stage": "runtime",
            "status": "working",
            "current_step": "awaiting_first_output",
        },
    )

    snapshot = snapshot_from_task_state(state)
    text = format_realtime_task_state_snapshot(snapshot)

    assert snapshot.pending_tool == {
        "tool_name": "product_search",
        "status": "working",
        "current_step": "product_search",
        "run_id": "run-1",
    }
    assert snapshot.tts_state == "speaking"
    assert snapshot.last_spoken_progress == {
        "text": "I am on it.",
        "source": "realtime_sla_fallback",
        "replaceable": True,
        "display_only": True,
        "current_step": "awaiting_first_output",
    }
    assert snapshot.last_realtime_event_ids == ["evt-tool-start", "evt-progress-1"]
    assert "pending_tool: product_search [working]" in text
    assert "last_spoken_progress: I am on it." in text


def test_realtime_task_state_reducer_bounds_recent_event_ids() -> None:
    state = RealtimeTaskState(task_id="rtask:u1:s1", user_id="u1", session_id="s1")

    for index in range(30):
        state = reduce_realtime_task_state_event(
            state,
            event_type="run.progress",
            payload={"event_id": f"evt-{index}", "status": "working"},
        )

    snapshot = snapshot_from_task_state(state)
    assert len(snapshot.last_realtime_event_ids) == 24
    assert snapshot.last_realtime_event_ids[0] == "evt-6"
    assert snapshot.last_realtime_event_ids[-1] == "evt-29"


def test_realtime_task_state_reducer_clears_pending_tool_on_finish_or_failure() -> None:
    state = RealtimeTaskState(
        task_id="rtask:u1:s1",
        user_id="u1",
        session_id="s1",
        pending_tool={
            "tool_name": "product_search",
            "status": "working",
            "current_step": "product_search",
            "run_id": "run-1",
        },
    )

    finished = reduce_realtime_task_state_event(
        state,
        event_type="tool.finished",
        payload={"event_id": "evt-tool-finished", "tool_name": "product_search"},
    )
    failed = reduce_realtime_task_state_event(
        state,
        event_type="tool.failed",
        payload={"event_id": "evt-tool-failed", "tool_name": "product_search"},
    )

    assert finished.pending_tool is None
    assert finished.last_realtime_event_ids == ["evt-tool-finished"]
    assert failed.pending_tool is None
    assert failed.last_realtime_event_ids == ["evt-tool-failed"]


def test_realtime_task_state_reducer_marks_cancel_and_hangup_sources() -> None:
    state = RealtimeTaskState(
        task_id="rtask:u1:s1",
        user_id="u1",
        session_id="s1",
        pending_tool={"tool_name": "product_search", "status": "working"},
        tts_state="speaking",
    )

    cancelled = reduce_realtime_task_state_event(
        state,
        event_type="run.cancel",
        payload={"event_id": "evt-cancel", "cancel_source": "gateway_cancel"},
    )
    hangup_cancelled = reduce_realtime_task_state_event(
        state,
        event_type="run.cancel",
        payload={"event_id": "evt-cancel-hangup", "cancel_source": "gateway_hangup"},
    )
    hung_up = reduce_realtime_task_state_event(
        state,
        event_type="call.hangup",
        payload={"event_id": "evt-hangup", "cancel_source": "gateway_hangup"},
    )

    assert cancelled.tts_state == "interrupted"
    assert cancelled.barge_in_source == "explicit_cancel"
    assert cancelled.pending_tool is None
    assert cancelled.last_realtime_event_ids == ["evt-cancel"]
    assert hangup_cancelled.tts_state == "interrupted"
    assert hangup_cancelled.barge_in_source == "hangup"
    assert hangup_cancelled.pending_tool is None
    assert hangup_cancelled.last_realtime_event_ids == ["evt-cancel-hangup"]
    assert hung_up.tts_state == "interrupted"
    assert hung_up.barge_in_source == "hangup"
    assert hung_up.pending_tool is None
    assert hung_up.last_realtime_event_ids == ["evt-hangup"]


def test_realtime_task_state_reducer_tracks_tts_idle_and_superseded_transitions() -> None:
    state = RealtimeTaskState(
        task_id="rtask:u1:s1",
        user_id="u1",
        session_id="s1",
        tts_state="speaking",
        last_spoken_progress={"text": "I am on it.", "replaceable": True},
        speech_turn_id="speech-1",
    )

    idle = reduce_realtime_task_state_event(
        state,
        event_type="tts.finished",
        payload={"event_id": "evt-tts-finished", "speech_turn_id": "speech-1"},
    )
    speaking = reduce_realtime_task_state_event(
        state,
        event_type="tts.started",
        payload={"event_id": "evt-tts-started", "speech_turn_id": "speech-1"},
    )
    superseded = reduce_realtime_task_state_event(
        state,
        event_type="tts.superseded",
        payload={"event_id": "evt-tts-superseded", "speech_turn_id": "speech-2"},
    )
    display_superseded = reduce_realtime_task_state_event(
        state,
        event_type="display.superseded",
        payload={"event_id": "evt-display-superseded"},
    )

    assert speaking.tts_state == "speaking"
    assert speaking.speech_turn_id == "speech-1"
    assert speaking.last_realtime_event_ids == ["evt-tts-started"]
    assert idle.tts_state == "idle"
    assert idle.speech_turn_id == "speech-1"
    assert idle.last_realtime_event_ids == ["evt-tts-finished"]
    assert superseded.tts_state == "superseded"
    assert superseded.speech_turn_id == "speech-2"
    assert superseded.last_realtime_event_ids == ["evt-tts-superseded"]
    assert display_superseded.tts_state == "superseded"
    assert display_superseded.last_realtime_event_ids == ["evt-display-superseded"]


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


def test_realtime_task_state_artifact_reuse_uses_tool_execution_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        "assistant_agent.services.realtime_task_state.tool_execution_policy",
        lambda tool_name: ToolExecutionPolicy(artifact_reuse="reusable")
        if tool_name == "calendar.search_events"
        else ToolExecutionPolicy(),
    )
    task_store = InMemoryRealtimeTaskStateStore()

    run_assistant_request(
        _realtime_request("查一下我的日程", run_id="run-1", turn_id="turn-1"),
        runtime=_CalendarSearchRuntime(),
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )
    prepared = prepare_realtime_task_state_request(
        _realtime_request(
            "等等，加上明天下午",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        store=task_store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    assert snapshot["latest_revision"]["strategy"] == "reuse_and_replan"
    assert snapshot["reusable_artifacts"][0]["tool_name"] == "calendar.search_events"


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


def test_realtime_task_state_records_checkpoint_for_multi_step_read_only_run() -> None:
    task_store = InMemoryRealtimeTaskStateStore()

    run_assistant_request(
        _realtime_request("帮我搜索并比价三款蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        runtime=_ProductSearchAndPriceCompareRuntime(),
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )
    state = task_store.get("u1", "s1")

    assert state is not None
    checkpoints = [artifact for artifact in state.artifacts if artifact.kind == "checkpoint"]
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint.reuse_policy == "reusable"
    assert checkpoint.summary == "Completed 2 reusable tool steps."
    assert checkpoint.context == {
        "schema_version": "realtime_checkpoint_v1",
        "completed_step_count": 2,
        "completed_tools": ["product_search", "price_compare"],
        "artifact_refs": [
            {
                "tool_name": "product_search",
                "output_ref": "mock://products/headphones",
                "summary": "通勤降噪耳机 A",
            },
            {
                "tool_name": "price_compare",
                "output_ref": "mock://prices/headphones",
                "summary": "Cheapest offer is 359 CNY.",
            },
        ],
    }
    assert "raw_provider_payload" not in str(checkpoint.model_dump(mode="json"))


def test_realtime_task_state_resumes_from_checkpoint_on_interrupt() -> None:
    task_store = InMemoryRealtimeTaskStateStore()
    conversation_store = InMemoryConversationStore()

    run_assistant_request(
        _realtime_request("帮我搜索并比价三款蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        runtime=_ProductSearchAndPriceCompareRuntime(),
        conversation_store=conversation_store,
        realtime_task_state_store=task_store,
        load_env=False,
    )
    sink = ListEventSink()
    artifacts = run_assistant_request(
        _realtime_request(
            "等等，把预算改成 400 元以内",
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

    snapshot = artifacts.state.request.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    checkpoint = next(artifact for artifact in snapshot["reusable_artifacts"] if artifact["kind"] == "checkpoint")
    progress = [event for event in artifacts.events if event.type == "tool_progress"][0]
    assert snapshot["latest_revision"]["strategy"] == "resume_from_checkpoint"
    assert snapshot["continuation_strategy"] == "resume_from_checkpoint"
    assert checkpoint["context"]["schema_version"] == "realtime_checkpoint_v1"
    assert checkpoint["context"]["completed_tools"] == ["product_search", "price_compare"]
    assert progress.text == "Resuming from the latest task checkpoint."
    assert progress.payload["strategy"] == "resume_from_checkpoint"
    assert progress.payload["checkpoint_count"] == 1


def test_realtime_task_state_marks_checkpoint_stale_when_user_restarts_work() -> None:
    task_store = InMemoryRealtimeTaskStateStore()

    run_assistant_request(
        _realtime_request("帮我搜索并比价三款蓝牙耳机", run_id="run-1", turn_id="turn-1"),
        runtime=_ProductSearchAndPriceCompareRuntime(),
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )
    prepared = prepare_realtime_task_state_request(
        _realtime_request(
            "重新搜索，全部换一批，不要之前结果",
            run_id="run-2",
            turn_id="turn-2",
            control="interrupt",
        ),
        store=task_store,
    )

    snapshot = prepared.metadata[REALTIME_TASK_STATE_METADATA_KEY]
    state = task_store.get("u1", "s1")
    assert snapshot["latest_revision"]["strategy"] == "restart"
    assert not any(artifact["kind"] == "checkpoint" for artifact in snapshot["reusable_artifacts"])
    assert state is not None
    assert any(artifact.kind == "checkpoint" and artifact.reuse_policy == "stale" for artifact in state.artifacts)


def test_run_assistant_request_updates_call_state_from_runtime_events() -> None:
    task_store = InMemoryRealtimeTaskStateStore()
    sink = ListEventSink()

    run_assistant_request(
        _realtime_request("帮我找通勤耳机", run_id="run-1", turn_id="turn-1"),
        runtime=_RealtimeEventRuntime(),
        event_sink=sink,
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )

    state = task_store.get("u1", "s1")
    assert state is not None
    assert [event.type for event in sink.events] == ["tool_started", "progress_message"]
    assert state.pending_tool == {
        "tool_name": "product_search",
        "status": "working",
        "current_step": "product_search",
        "run_id": "run-1",
    }
    assert state.tts_state == "speaking"
    assert state.last_spoken_progress == {
        "text": "I will check that.",
        "source": "native_tool_wait",
        "replaceable": True,
        "display_only": True,
        "current_step": "product_search",
    }
    assert state.last_realtime_event_ids == ["evt-tool-start", "evt-progress"]


def test_run_assistant_request_clears_pending_tool_from_runtime_completion_events() -> None:
    task_store = InMemoryRealtimeTaskStateStore()
    sink = ListEventSink()

    run_assistant_request(
        _realtime_request("帮我找通勤耳机", run_id="run-1", turn_id="turn-1"),
        runtime=_RealtimeToolCompletionRuntime(),
        event_sink=sink,
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )

    state = task_store.get("u1", "s1")
    assert state is not None
    assert [event.type for event in sink.events] == ["tool_started", "tool_finished"]
    assert state.pending_tool is None
    assert state.last_realtime_event_ids == ["evt-tool-start", "evt-tool-finished"]


def test_run_assistant_request_updates_tts_and_hangup_lifecycle_from_runtime_events() -> None:
    task_store = InMemoryRealtimeTaskStateStore()
    sink = ListEventSink()

    run_assistant_request(
        _realtime_request("帮我找通勤耳机", run_id="run-1", turn_id="turn-1"),
        runtime=_RealtimeTtsLifecycleRuntime(),
        event_sink=sink,
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )

    state = task_store.get("u1", "s1")
    assert state is not None
    assert [event.type for event in sink.events] == [
        "progress_message",
        "tts_started",
        "tts_finished",
        "tts_superseded",
        "call_hangup",
    ]
    assert state.tts_state == "interrupted"
    assert state.barge_in_source == "hangup"
    assert state.pending_tool is None
    assert state.speech_turn_id == "speech-2"
    assert state.last_realtime_event_ids == [
        "evt-progress",
        "evt-tts-started",
        "evt-tts-finished",
        "evt-tts-superseded",
        "evt-hangup",
    ]


def test_run_assistant_request_reads_hangup_source_from_task_cancelled_error_detail() -> None:
    task_store = InMemoryRealtimeTaskStateStore()
    sink = ListEventSink()

    run_assistant_request(
        _realtime_request("帮我找通勤耳机", run_id="run-1", turn_id="turn-1"),
        runtime=_RealtimeCancelledFromHangupRuntime(),
        event_sink=sink,
        conversation_store=InMemoryConversationStore(),
        realtime_task_state_store=task_store,
        load_env=False,
    )

    state = task_store.get("u1", "s1")
    assert state is not None
    assert [event.type for event in sink.events] == ["tool_started", "task_cancelled"]
    assert state.pending_tool is None
    assert state.tts_state == "interrupted"
    assert state.barge_in_source == "hangup"
    assert state.last_realtime_event_ids == ["evt-tool-start", "evt-task-cancelled"]


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


class _ProductSearchAndPriceCompareRuntime:
    def run_state(self, request: UserRequest, **kwargs) -> AgentState:
        state = AgentState.from_request(request)
        state.tool_results.extend(
            [
                ToolResult(
                    tool_name="product_search",
                    success=True,
                    output_ref="mock://products/headphones",
                    data={
                        "provider": "mock",
                        "query_used": "蓝牙耳机 500 元以内",
                        "items": [
                            {
                                "product_id": "p1",
                                "title": "通勤降噪耳机 A",
                                "price": 399,
                                "currency": "CNY",
                                "raw_provider_payload": {"token": "sk-test"},
                            }
                        ],
                    },
                ),
                ToolResult(
                    tool_name="price_compare",
                    success=True,
                    output_ref="mock://prices/headphones",
                    data={
                        "summary": "Cheapest offer is 359 CNY.",
                        "items": [
                            {
                                "product_id": "p1",
                                "title": "通勤降噪耳机 A",
                                "price": 359,
                                "currency": "CNY",
                                "raw_provider_payload": {"token": "sk-test"},
                            }
                        ],
                    },
                ),
            ]
        )
        state.set_response(AgentResponse(message="已完成搜索和比价。"))
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


class _CalendarSearchRuntime:
    def run_state(self, request: UserRequest, **kwargs) -> AgentState:
        state = AgentState.from_request(request)
        state.tool_results.append(
            ToolResult(
                tool_name="calendar.search_events",
                success=True,
                output_ref="calendar://events/tomorrow",
                data={
                    "summary": "Found two calendar events tomorrow.",
                    "side_effect": {
                        "level": "external_read",
                        "requires_confirmation": False,
                        "description": "Reads calendar events without writing.",
                    },
                },
            )
        )
        state.set_response(AgentResponse(message="找到两个日程。"))
        return state


class _RealtimeEventRuntime:
    def run_state(self, request: UserRequest, event_sink: ListEventSink, **kwargs) -> AgentState:
        event_sink.emit(
            AgentEvent(
                type="tool_started",
                session_id=request.session_id,
                run_id="run-1",
                tool_name="product_search",
                payload={"event_id": "evt-tool-start", "current_step": "product_search"},
            )
        )
        event_sink.emit(
            AgentEvent(
                type="progress_message",
                session_id=request.session_id,
                run_id="run-1",
                tool_name="product_search",
                text="I will check that.",
                payload={
                    "event_id": "evt-progress",
                    "source": "native_tool_wait",
                    "replaceable": True,
                    "display_only": True,
                    "current_step": "product_search",
                },
            )
        )
        state = AgentState.from_request(request)
        state.set_response(AgentResponse(message="找到结果。"))
        return state


class _RealtimeToolCompletionRuntime:
    def run_state(self, request: UserRequest, event_sink: ListEventSink, **kwargs) -> AgentState:
        event_sink.emit(
            AgentEvent(
                type="tool_started",
                session_id=request.session_id,
                run_id="run-1",
                tool_name="product_search",
                payload={"event_id": "evt-tool-start", "current_step": "product_search"},
            )
        )
        event_sink.emit(
            AgentEvent(
                type="tool_finished",
                session_id=request.session_id,
                run_id="run-1",
                tool_name="product_search",
                output_ref="mock://products/headphones",
                payload={"event_id": "evt-tool-finished"},
            )
        )
        state = AgentState.from_request(request)
        state.set_response(AgentResponse(message="找到结果。"))
        return state


class _RealtimeTtsLifecycleRuntime:
    def run_state(self, request: UserRequest, event_sink: ListEventSink, **kwargs) -> AgentState:
        event_sink.emit(
            AgentEvent(
                type="progress_message",
                session_id=request.session_id,
                run_id="run-1",
                text="I am on it.",
                payload={
                    "event_id": "evt-progress",
                    "source": "native_tool_wait",
                    "replaceable": True,
                    "display_only": True,
                    "speech_turn_id": "speech-1",
                },
            )
        )
        event_sink.emit(
            AgentEvent(
                type="tts_started",
                session_id=request.session_id,
                run_id="run-1",
                payload={"event_id": "evt-tts-started", "speech_turn_id": "speech-1"},
            )
        )
        event_sink.emit(
            AgentEvent(
                type="tts_finished",
                session_id=request.session_id,
                run_id="run-1",
                payload={"event_id": "evt-tts-finished", "speech_turn_id": "speech-1"},
            )
        )
        event_sink.emit(
            AgentEvent(
                type="tts_superseded",
                session_id=request.session_id,
                run_id="run-1",
                payload={"event_id": "evt-tts-superseded", "speech_turn_id": "speech-2"},
            )
        )
        event_sink.emit(
            AgentEvent(
                type="call_hangup",
                session_id=request.session_id,
                run_id="run-1",
                payload={"event_id": "evt-hangup", "cancel_source": "gateway_hangup"},
            )
        )
        state = AgentState.from_request(request)
        state.set_response(AgentResponse(message="已停止。"))
        return state


class _RealtimeCancelledFromHangupRuntime:
    def run_state(self, request: UserRequest, event_sink: ListEventSink, **kwargs) -> AgentState:
        event_sink.emit(
            AgentEvent(
                type="tool_started",
                session_id=request.session_id,
                run_id="run-1",
                tool_name="product_search",
                payload={"event_id": "evt-tool-start", "current_step": "product_search"},
            )
        )
        event_sink.emit(
            AgentEvent(
                type="task_cancelled",
                session_id=request.session_id,
                run_id="run-1",
                error={
                    "code": "AGENT_RUN_CANCELLED",
                    "message": "Agent run cancelled.",
                    "detail": {
                        "cancel_source": "gateway_hangup",
                        "cancel_reason": "call_hangup",
                    },
                },
                payload={"event_id": "evt-task-cancelled"},
            )
        )
        state = AgentState.from_request(request)
        state.cancel(
            details={
                "cancel_source": "gateway_hangup",
                "cancel_reason": "call_hangup",
            }
        )
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
        "interaction_mode": "realtime",
        "realtime": {"run_id": run_id, "turn_id": turn_id},
    }
    if control:
        metadata["control"] = control
    return UserRequest(user_id="u1", session_id="s1", text=text, metadata=metadata)


def _arbitration(*, disposition: str, revision_type: str | None):
    request = RealtimeTurnArbitrationRequest(
        decision_id="decision-task-state",
        user_id="u1",
        session_id="s1",
        turn_id="turn-2",
        run_id="run-2",
        expected_run_id="run-1",
        utterance="semantic revision",
        task_state={},
    )
    decision = normalize_arbitration_decision(
        {
            "disposition": disposition,
            "revision_type": revision_type,
            "confidence": 0.99,
            "reason_code": "task_state_test",
        },
        request=request,
        min_confidence=0.80,
        source="semantic_llm",
    )
    return request, decision


def _semantic_interrupt_request(
    text: str,
    *,
    run_id: str,
    turn_id: str,
    disposition: str,
    revision_type: str | None,
) -> UserRequest:
    _, decision = _arbitration(
        disposition=disposition,
        revision_type=revision_type,
    )
    decision = decision.model_copy(
        update={
            "decision_id": f"decision-{turn_id}",
            "expected_run_id": "run-1",
        }
    )
    return UserRequest(
        user_id="u1",
        session_id="s1",
        text=text,
        metadata={
            "source": "realtime_media_websocket",
            "interaction_mode": "realtime",
            "control": "interrupt",
            "realtime": {"run_id": run_id, "turn_id": turn_id},
            REALTIME_TURN_ARBITRATION_METADATA_KEY: decision.model_dump(mode="json"),
        },
    )

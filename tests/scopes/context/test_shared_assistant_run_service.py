import asyncio

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentError, AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services import assistant_run_service as run_service
from assistant_agent.services.assistant_run_service import (
    ConversationTurn,
    InMemoryConversationStore,
    JsonlConversationStore,
    clear_conversation_history,
    clear_user_conversation_history,
    run_assistant_query,
    run_assistant_request,
    run_assistant_request_stream,
)
from assistant_agent.services.context.builder import build_assistant_context_pack
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.services import trace_conversation
from assistant_agent.services.trace_conversation import InMemoryTraceConversationStore


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


class MutableCancelToken:
    def __init__(self, cancelled: bool = False, metadata: dict[str, object] | None = None) -> None:
        self.cancelled = cancelled
        self._metadata = dict(metadata or {})

    def is_cancelled(self) -> bool:
        return self.cancelled

    @property
    def cancel_metadata(self) -> dict[str, object]:
        return dict(self._metadata)


def test_shared_assistant_run_service_returns_cli_and_api_shapes() -> None:
    artifacts = run_assistant_query(
        "生成一张白色运动鞋的电商主图",
        load_env=False,
        conversation_store=InMemoryConversationStore(),
    )

    api_response = artifacts.api_response()
    cli_payload = artifacts.cli_payload()

    assert api_response.response_text
    assert api_response.react_steps
    assert api_response.decision_trace
    assert api_response.runtime_info["providers"]["chat"] == "mock"
    assert api_response.current_stage in {"final_answer", "final_response"}
    assert cli_payload["response_text"] == api_response.response_text
    assert cli_payload["react_steps"] == api_response.react_steps
    assert cli_payload["decision_trace"] == api_response.decision_trace
    assert cli_payload["runtime_info"] == api_response.runtime_info


def test_shared_assistant_run_service_accepts_user_request() -> None:
    artifacts = run_assistant_request(
        UserRequest(user_id="u1", session_id="s1", text="你好"),
        load_env=False,
        conversation_store=InMemoryConversationStore(),
    )

    response = artifacts.api_response()

    assert response.run_id.startswith("run_")
    assert response.trace_id.startswith("trace_")
    assert response.runtime_info["graph_mode"] == "assistant_loop"
    assert response.current_stage


def test_failed_turn_records_trace_debug_content_without_conversation_history(monkeypatch) -> None:
    conversation_store = InMemoryConversationStore()
    trace_conversation_store = InMemoryTraceConversationStore()
    request = UserRequest(user_id="u1", session_id="s1", text="帮我查一下今天的 AI 新闻")
    state = AgentState.from_request(request)
    state.status = "failed"
    state.errors.append(
        AgentError(
            message="provider_network_error: SSL EOF",
            source="web_search",
            details={"code": "provider_network_error"},
        )
    )
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    monkeypatch.setattr(
        trace_conversation,
        "get_default_trace_conversation_store",
        lambda: trace_conversation_store,
    )

    run_service._record_conversation_turn(
        state,
        conversation_store=conversation_store,
        enable_conversation_history=True,
    )
    run_service._record_trace_conversation_turn(state)

    assert conversation_store.get("u1", "s1") == []
    view = trace_conversation_store.get(
        user_id="u1",
        session_id="s1",
        trace_id=state.trace_id,
    )
    assert view is not None
    assert view.user.text == "帮我查一下今天的 AI 新闻"
    assert view.assistant.text == "请求失败：provider_network_error: SSL EOF"


def test_completed_turn_does_not_duplicate_trace_debug_content(monkeypatch) -> None:
    trace_conversation_store = InMemoryTraceConversationStore()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="你好"))
    state.status = "completed"
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    monkeypatch.setattr(
        trace_conversation,
        "get_default_trace_conversation_store",
        lambda: trace_conversation_store,
    )

    run_service._record_trace_conversation_turn(state)

    assert (
        trace_conversation_store.get(
            user_id="u1",
            session_id="s1",
            trace_id=state.trace_id,
        )
        is None
    )


def test_shared_assistant_run_service_traces_conversation_preparation_before_run() -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)

    artifacts = run_assistant_request(
        UserRequest(user_id="u1", session_id="s1", text="不要把这句话写入准备事件"),
        runtime=runtime,
        load_env=False,
        conversation_store=InMemoryConversationStore(),
    )

    events = trace_store.list_by_run(artifacts.state.run_id)
    canonical = [event.canonical_event for event in events]
    prepare_event = next(event for event in events if event.canonical_event == "conversation.prepare.finished")
    assert canonical.index("conversation.prepare.finished") < canonical.index("run.started")
    assert isinstance(prepare_event.latency_ms, int)
    assert prepare_event.latency_ms >= 0
    assert prepare_event.attributes["conversation_turn_index"] == 1
    assert "不要把这句话写入准备事件" not in prepare_event.model_dump_json()


def test_run_assistant_request_stream_yields_events_and_returns_artifacts() -> None:
    async def scenario() -> tuple[list[str], str, list[str], int]:
        store = InMemoryConversationStore()
        stream = run_assistant_request_stream(
            UserRequest(user_id="u1", session_id="s1", text="你好"),
            load_env=False,
            conversation_store=store,
        )

        events = [event async for event in stream]
        artifacts = await stream.result()
        return (
            [event.type for event in events],
            artifacts.state.status,
            [event.type for event in artifacts.events],
            len(store.get("u1", "s1")),
        )

    streamed_types, status, artifact_types, stored_turns = asyncio.run(scenario())

    assert status == "completed"
    assert streamed_types[0] == "task_started"
    assert "response_delta" in streamed_types
    assert streamed_types[-1] == "final_response"
    assert streamed_types == artifact_types
    assert stored_turns == 1


def test_run_assistant_request_stream_preserves_compatibility_event_sink() -> None:
    async def scenario() -> tuple[list[str], list[str], list[str]]:
        compatibility_sink = RecordingSink()
        stream = run_assistant_request_stream(
            UserRequest(user_id="u1", session_id="s1", text="你好"),
            load_env=False,
            conversation_store=InMemoryConversationStore(),
            event_sink=compatibility_sink,
        )

        events = [event async for event in stream]
        artifacts = await stream.result()
        return (
            [event.type for event in events],
            [event.type for event in artifacts.events],
            [event.type for event in compatibility_sink.events],
        )

    streamed_types, artifact_types, compatibility_types = asyncio.run(scenario())

    assert streamed_types
    assert streamed_types == artifact_types == compatibility_types


def test_run_assistant_request_stream_pre_graph_cancel_returns_cancelled_artifacts() -> None:
    async def scenario() -> tuple[list[str], str, str]:
        token = MutableCancelToken(
            cancelled=True,
            metadata={"cancel_source": "gateway", "cancel_reason": "client_disconnect"},
        )
        stream = run_assistant_request_stream(
            UserRequest(user_id="u1", session_id="s1", text="hello"),
            load_env=False,
            conversation_store=InMemoryConversationStore(),
            cancel_token=token,
        )

        events = [event async for event in stream]
        artifacts = await stream.result()
        return (
            [event.type for event in events],
            artifacts.state.status,
            artifacts.state.errors[-1].details["cancel_source"],
        )

    event_types, status, cancel_source = asyncio.run(scenario())

    assert event_types == ["task_started", "task_cancelled"]
    assert status == "cancelled"
    assert cancel_source == "gateway"


def test_shared_assistant_run_service_injects_multi_turn_history() -> None:
    store = InMemoryConversationStore()

    first = run_assistant_query(
        "我喜欢白色低帮运动鞋",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
    )
    second = run_assistant_query(
        "基于刚才的信息，生成一句商品标题",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
    )

    history = second.state.request.metadata["conversation_history"]
    context_text = second.state.request.metadata["conversation_context_text"]

    assert len(history) == 1
    assert history[0]["user_text"] == "我喜欢白色低帮运动鞋"
    assert history[0]["assistant_text"] == first.api_response().response_text
    assert "我喜欢白色低帮运动鞋" in context_text
    assert second.state.request.metadata["conversation_turn_index"] == 2


def test_shared_assistant_run_service_compacts_older_conversation_context() -> None:
    store = InMemoryConversationStore()
    for index in range(4):
        store.append(
            "u1",
            "s1",
            ConversationTurn(
                user_text=f"第 {index + 1} 轮用户说了很多偏好内容" + ("长文本" * 40),
                assistant_text=f"第 {index + 1} 轮助手回复" + ("详细说明" * 40),
                run_id=f"run_{index + 1}",
                trace_id=f"trace_{index + 1}",
            ),
        )

    artifacts = run_assistant_query(
        "请基于最近对话继续",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
    )
    metadata = artifacts.state.request.metadata
    context_text = metadata["conversation_context_text"]
    pack = build_assistant_context_pack(
        state=artifacts.state,
        observations=[],
        tool_specs=[],
        iteration=0,
        max_iterations=5,
    )

    assert len(metadata["conversation_history"]) == 4
    assert metadata["conversation_turn_index"] == 5
    assert metadata["conversation_context_compacted"] is True
    assert metadata["conversation_context_compacted_turns"] == 2
    assert metadata["conversation_context_token_aware"] is True
    assert metadata["conversation_context_recent_tokens"] > 0
    assert metadata["conversation_context_recent_token_budget"] > 0
    assert "较早对话摘要" in context_text
    assert "最近对话原文" in context_text
    assert "3. 用户：第 3 轮用户" in context_text
    assert "4. 用户：第 4 轮用户" in context_text
    assert pack.source_counts["conversation_turns"] == 4
    assert pack.source_counts["conversation_compacted_turns"] == 2


def test_shared_assistant_run_service_keeps_short_recent_transcript_token_aware() -> None:
    store = InMemoryConversationStore()
    for index in range(4):
        store.append(
            "u1",
            "s1",
            ConversationTurn(
                user_text=f"短用户 {index + 1}",
                assistant_text=f"短助手 {index + 1}",
                run_id=f"run_{index + 1}",
                trace_id=f"trace_{index + 1}",
            ),
        )

    artifacts = run_assistant_query(
        "请基于最近对话继续",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
    )
    metadata = artifacts.state.request.metadata

    summary = store.get_summary("u1", "s1")
    assert summary is None or summary.source_turn_count == 0
    assert metadata["conversation_context_token_aware"] is True
    assert metadata["conversation_context_recent_turns"] == 4
    assert metadata["conversation_context_compacted_turns"] == 0
    assert metadata["conversation_context_compacted"] is False
    assert "1. 用户：短用户 1" in metadata["conversation_context_text"]
    assert "4. 用户：短用户 4" in metadata["conversation_context_text"]


def test_shared_assistant_run_service_persists_and_restores_session_summary() -> None:
    store = InMemoryConversationStore()
    for index in range(3):
        store.append(
            "u1",
            "s1",
            ConversationTurn(
                user_text=f"第 {index + 1} 轮用户：必须保留约束",
                assistant_text=f"第 {index + 1} 轮助手：已确认",
                run_id=f"run_{index + 1}",
                trace_id=f"trace_{index + 1}",
            ),
        )

    first = run_assistant_query(
        "继续",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
        metadata={"conversation_recent_max_tokens": 1},
    )
    restored = run_assistant_query(
        "再继续",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
        metadata={"conversation_recent_max_tokens": 1},
    )

    assert store.get_summary("u1", "s1") is not None
    assert first.state.request.metadata["context_summary_present"] is True
    assert restored.state.request.metadata["context_summary_present"] is True
    assert restored.state.request.metadata["conversation_context_recent_turns"] == 2
    assert "较早对话摘要" in restored.state.request.metadata["conversation_context_text"]


def test_session_summary_rolls_forward_without_resummarizing_old_turns() -> None:
    store = InMemoryConversationStore()
    for index in range(3):
        store.append(
            "u1",
            "s1",
            ConversationTurn(
                user_text=f"第 {index + 1} 轮用户：必须保留约束",
                assistant_text=f"第 {index + 1} 轮助手：已确认",
                run_id=f"run_{index + 1}",
                trace_id=f"trace_{index + 1}",
            ),
        )

    first = run_assistant_query(
        "继续第一步",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
        metadata={"conversation_recent_max_tokens": 1},
    )
    first_summary = store.get_summary("u1", "s1")

    second = run_assistant_query(
        "继续第二步",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
        metadata={"conversation_recent_max_tokens": 1},
    )
    second_summary = store.get_summary("u1", "s1")

    assert first_summary is not None
    assert first_summary.source_turn_count == 1
    assert second_summary is not None
    assert second_summary.source_turn_count == 2
    assert second_summary.decisions.count("第 1 轮助手：已确认") == 1
    assert "第 2 轮助手：已确认" in second_summary.decisions
    assert "run:run_1" in second_summary.important_refs
    assert "run:run_2" in second_summary.important_refs
    assert first.state.request.metadata["conversation_context_compacted_turns"] == 1
    assert second.state.request.metadata["conversation_context_compacted_turns"] == 2
    assert "第 3 轮助手：已确认" not in second_summary.decisions


def test_explicit_compact_uses_selected_recent_window_without_resummarizing_raw_recent() -> None:
    store = InMemoryConversationStore()
    for index in range(4):
        store.append(
            "u1",
            "s1",
            ConversationTurn(
                user_text=f"第 {index + 1} 轮用户",
                assistant_text=f"第 {index + 1} 轮助手",
                run_id=f"run_{index + 1}",
                trace_id=f"trace_{index + 1}",
            ),
        )

    compacted = run_assistant_query(
        "/compact",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
        metadata={"compact_context": True},
    )
    summary = store.get_summary("u1", "s1")

    assert summary is not None
    assert summary.source_turn_count == 2
    assert "第 1 轮助手" in summary.decisions
    assert "第 2 轮助手" in summary.decisions
    assert "第 3 轮助手" not in summary.decisions
    assert "第 4 轮助手" not in summary.decisions
    assert compacted.state.request.metadata["conversation_context_recent_turns"] == 2


def test_reset_conversation_clears_session_summary_before_current_turn() -> None:
    store = InMemoryConversationStore()
    for index in range(3):
        store.append(
            "u1",
            "s1",
            ConversationTurn(
                user_text=f"第 {index + 1} 轮用户",
                assistant_text=f"第 {index + 1} 轮助手",
                run_id=f"run_{index + 1}",
                trace_id=f"trace_{index + 1}",
            ),
        )
    run_assistant_query(
        "生成摘要",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
        metadata={"context_budget_max_chars": 50_000},
    )
    assert store.get_summary("u1", "s1") is not None

    reset = run_assistant_query(
        "重置后继续",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
        metadata={"reset_conversation": True, "context_budget_max_chars": 50_000},
    )

    assert reset.state.request.metadata["conversation_history"] == []
    assert reset.state.request.metadata.get("context_summary_present") is None
    assert store.get_summary("u1", "s1") is None


def test_session_summary_does_not_write_to_memory_store() -> None:
    memory_store = InMemoryStore()
    conversation_store = InMemoryConversationStore()
    runtime = AgentGraphRuntime(memory_store=memory_store)
    for index in range(3):
        conversation_store.append(
            "u1",
            "s1",
            ConversationTurn(
                user_text=f"第 {index + 1} 轮用户：必须保留约束",
                assistant_text=f"第 {index + 1} 轮助手：已确认",
                run_id=f"run_{index + 1}",
                trace_id=f"trace_{index + 1}",
            ),
        )

    run_assistant_request(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="继续",
            metadata={"conversation_recent_max_tokens": 1},
        ),
        load_env=False,
        runtime=runtime,
        conversation_store=conversation_store,
    )

    assert conversation_store.get_summary("u1", "s1") is not None
    assert all(item.source != "context_summary" for item in memory_store.list_by_user("u1"))


def test_shared_assistant_run_service_keeps_sessions_isolated() -> None:
    store = InMemoryConversationStore()

    run_assistant_query(
        "记住这个偏好：极简风格",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
    )
    isolated = run_assistant_query(
        "这里应该没有上一段历史",
        user_id="u1",
        session_id="s2",
        load_env=False,
        conversation_store=store,
    )

    assert isolated.state.request.metadata["conversation_history"] == []
    assert isolated.state.request.metadata["conversation_context_text"] == ""


def test_shared_assistant_run_service_can_clear_multi_turn_history() -> None:
    store = InMemoryConversationStore()
    run_assistant_query(
        "第一轮",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
    )

    clear_conversation_history("u1", "s1", conversation_store=store)
    next_turn = run_assistant_query(
        "清空后的一轮",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
    )

    assert next_turn.state.request.metadata["conversation_history"] == []


def test_shared_assistant_run_service_persists_multi_turn_history_to_jsonl(tmp_path) -> None:
    path = tmp_path / "conversation_history.jsonl"
    first_store = JsonlConversationStore(path)

    first = run_assistant_query(
        "我喜欢白色低帮运动鞋",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=first_store,
    )

    restarted_store = JsonlConversationStore(path)
    second = run_assistant_query(
        "基于刚才的信息，生成一句商品标题",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=restarted_store,
    )

    history = second.state.request.metadata["conversation_history"]
    context_text = second.state.request.metadata["conversation_context_text"]

    assert path.exists()
    assert len(history) == 1
    assert history[0]["user_text"] == "我喜欢白色低帮运动鞋"
    assert history[0]["assistant_text"] == first.api_response().response_text
    assert "我喜欢白色低帮运动鞋" in context_text
    assert second.state.request.metadata["conversation_turn_index"] == 2


def test_shared_assistant_run_service_uses_configured_jsonl_conversation_store(tmp_path) -> None:
    path = tmp_path / "configured_conversation_history.jsonl"
    config = ProviderConfig(
        conversation_history_backend="jsonl",
        conversation_history_path=str(path),
        max_conversation_history_turns=2,
    )

    run_assistant_query(
        "第一轮",
        user_id="u1",
        session_id="s1",
        config=config,
        load_env=False,
    )
    second = run_assistant_query(
        "第二轮",
        user_id="u1",
        session_id="s1",
        config=config,
        load_env=False,
    )

    assert path.exists()
    assert second.state.request.metadata["conversation_turn_index"] == 2
    assert second.state.request.metadata["conversation_history"][0]["user_text"] == "第一轮"


def test_shared_assistant_run_service_clears_configured_jsonl_user_history(tmp_path) -> None:
    path = tmp_path / "clear_conversation_history.jsonl"
    config = ProviderConfig(conversation_history_backend="jsonl", conversation_history_path=str(path))

    run_assistant_query("s1 第一轮", user_id="u1", session_id="s1", config=config, load_env=False)
    run_assistant_query("s2 第一轮", user_id="u1", session_id="s2", config=config, load_env=False)

    assert clear_user_conversation_history("u1", config=config) == 2

    next_turn = run_assistant_query(
        "清空后的一轮",
        user_id="u1",
        session_id="s1",
        config=config,
        load_env=False,
    )
    assert next_turn.state.request.metadata["conversation_history"] == []


def test_configured_jsonl_conversation_store_resolves_relative_path_from_repo_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_service, "REPO_ROOT", tmp_path)
    config = ProviderConfig(
        conversation_history_backend="jsonl",
        conversation_history_path="relative/conversation_history.jsonl",
    )

    store = run_service.get_default_conversation_store(config)
    store.append(
        "u1",
        "s1",
        ConversationTurn(
            user_text="第一轮",
            assistant_text="收到",
            run_id="run_1",
            trace_id="trace_1",
        ),
    )

    assert (tmp_path / "relative" / "conversation_history.jsonl").exists()

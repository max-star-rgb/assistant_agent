from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
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
)
from assistant_agent.services.context.builder import build_assistant_context_pack


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
    assert "较早对话摘要" in context_text
    assert "最近对话原文" in context_text
    assert "3. 用户：第 3 轮用户" in context_text
    assert "4. 用户：第 4 轮用户" in context_text
    assert pack.source_counts["conversation_turns"] == 4
    assert pack.source_counts["conversation_compacted_turns"] == 2


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
        metadata={"context_budget_max_chars": 50_000},
    )
    restored = run_assistant_query(
        "再继续",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
        metadata={"context_budget_max_chars": 50_000},
    )

    assert store.get_summary("u1", "s1") is not None
    assert first.state.request.metadata["context_summary_present"] is True
    assert restored.state.request.metadata["context_summary_present"] is True
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
        metadata={"context_budget_max_chars": 50_000},
    )
    first_summary = store.get_summary("u1", "s1")

    second = run_assistant_query(
        "继续第二步",
        user_id="u1",
        session_id="s1",
        load_env=False,
        conversation_store=store,
        metadata={"context_budget_max_chars": 50_000},
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
            metadata={"context_budget_max_chars": 50_000},
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

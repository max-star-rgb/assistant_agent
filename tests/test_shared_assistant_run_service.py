from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.assistant_run_service import (
    InMemoryConversationStore,
    clear_conversation_history,
    run_assistant_query,
    run_assistant_request,
)


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

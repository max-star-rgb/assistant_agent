from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.requests import UserRequest


def _run(request: UserRequest):
    return AgentGraphRuntime(memory_store=InMemoryStore()).run_state(request)


def test_direct_chat_returns_chat_response_text() -> None:
    state = _run(UserRequest(user_id="u1", session_id="s1", text="帮我写一段商品介绍"))

    assert state.intent is not None
    assert state.intent.intent == "direct_chat"
    assert state.tool_results == []
    assert state.response is not None
    assert "离线 mock direct_chat 回复" in state.response.message


def test_image_generation_response_is_specific() -> None:
    state = _run(UserRequest(user_id="u1", session_id="s1", text="生成一张日系海报"))

    assert state.response is not None
    assert "生成图片" in state.response.message
    assert "local://generated/poster.png" in state.response.message
    assert "已完成请求处理" not in state.response.message


def test_product_search_price_compare_response_is_specific() -> None:
    state = _run(UserRequest(user_id="u1", session_id="s1", text="帮我找 500 元以内的白鞋，再比较价格"))

    assert state.response is not None
    assert "找到" in state.response.message
    assert "完成比价" in state.response.message
    assert "最低价格" in state.response.message
    assert "259.0 CNY" in state.response.message
    assert "mock-shop-b" in state.response.message


def test_ask_followup_response_is_clear_question() -> None:
    state = _run(UserRequest(user_id="u1", session_id="s1", text="这个"))

    assert state.intent is not None
    assert state.intent.intent == "ask_followup"
    assert state.response is not None
    assert "请补充" in state.response.message
    assert state.response.followup_question

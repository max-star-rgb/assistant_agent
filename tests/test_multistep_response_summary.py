from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest


def _run(request: UserRequest):
    return AgentGraphRuntime(memory_store=InMemoryStore()).run_state(request)


def test_image_search_compare_generate_response_summarizes_steps_in_order() -> None:
    state = _run(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我找图里的鞋，比较价格，再生成一张海报",
            image_ids=["img1"],
        )
    )

    message = state.response.message

    assert "我先理解了图片内容" in message
    assert "已基于 mock 数据找到" in message
    assert "已完成比价" in message
    assert "已根据你的需求生成图片" in message
    assert message.index("我先理解了图片内容") < message.index("已基于 mock 数据找到")
    assert message.index("已基于 mock 数据找到") < message.index("已完成比价")
    assert message.index("已完成比价") < message.index("已根据你的需求生成图片")


def test_product_search_render_response_summarizes_render_output() -> None:
    state = _run(
        UserRequest(user_id="u1", session_id="s1", text="找一把黑色办公椅，然后放到现代办公室里看看")
    )

    message = state.response.message

    assert "已基于 mock 数据找到" in message
    assert "创建 3D 场景预览" in message
    assert "mock://render/preview.png" in message
    assert "已完成请求处理" not in message


def test_partial_failure_response_mentions_success_and_failure() -> None:
    state = _run(UserRequest(user_id="u1", session_id="s1", text="哪个便宜"))

    assert state.status == "failed"
    assert state.response is not None
    assert "处理失败" in state.response.message
    assert "price_compare 失败" in state.response.message
    assert "缺少商品候选列表" in state.response.message

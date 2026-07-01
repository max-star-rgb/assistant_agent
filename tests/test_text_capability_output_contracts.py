from assistant_agent.agent.prompt_builder import build_text_capability_output
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest


def test_text_capability_output_contract_shape() -> None:
    payload = build_text_capability_output(
        capability="image_generation",
        status="succeeded",
        output_ref="mock://image/1",
        data={"image_url": "mock://image/1", "prompt_used": "生成海报"},
        errors=[],
    )

    assert payload == {
        "capability": "image_generation",
        "status": "succeeded",
        "output_ref": "mock://image/1",
        "data": {"image_url": "mock://image/1", "prompt_used": "生成海报"},
        "errors": [],
    }


def test_direct_chat_response_exposes_stable_contract_without_raw_provider_payload() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="解释一下 Agent 和 Tool 的区别")
    )

    assert state.response is not None
    contract = state.response.data["contract"]
    assert contract["capability"] == "direct_chat"
    assert contract["status"] == "succeeded"
    assert contract["output_ref"] == "mock://chat/direct"
    assert "raw" not in contract
    assert "provider_response" not in contract


def test_image_generation_response_exposes_stable_contract_without_raw_provider_payload() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="生成一张赛博朋克风格海报")
    )

    assert state.tool_results
    contract = state.tool_results[0].data["contract"]
    assert contract["capability"] == "image_generation"
    assert contract["status"] == "succeeded"
    assert contract["output_ref"] == "local://generated/poster.png"
    assert contract["data"]["image_url"] == "local://generated/poster.png"
    assert "raw" not in contract
    assert "provider_response" not in contract

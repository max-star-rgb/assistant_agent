from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.capability_output import CapabilityOutputContract
from assistant_agent.schemas.requests import UserRequest


def test_direct_chat_response_has_capability_contract() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我写一段商品介绍")
    )

    contract = state.response.data["contract"]

    assert contract["capability"] == "direct_chat"
    assert contract["status"] == "succeeded"
    assert contract["output_ref"] == "mock://chat/direct"


def test_single_tool_capabilities_emit_contracts() -> None:
    cases = [
        (
            UserRequest(user_id="u1", session_id="s1", text="生成一张日系海报"),
            "image_generation",
            "image_generation",
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="看看图里有什么", image_ids=["img1"]),
            "vision_understanding",
            "image_understanding",
        ),
            (
                UserRequest(user_id="u1", session_id="s1", text="总结这个视频", video_ids=["v1"]),
                "video_understanding",
                "video_understanding",
            ),
        (
            UserRequest(user_id="u1", session_id="s1", text="帮我找白色运动鞋"),
            "shopping_search",
            "shopping_search",
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="把浅灰色沙发放到北欧风客厅看看"),
            "render_3d",
            "render_3d",
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="上次那个黑色包"),
            "memory_retrieval",
            "memory_retrieval",
        ),
    ]

    for request, expected_tool, expected_capability in cases:
        state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(request)
        result = state.tool_results[0]

        assert result.tool_name == expected_tool
        assert result.contract is not None
        assert isinstance(result.contract, CapabilityOutputContract)
        assert result.contract.capability == expected_capability
        assert result.contract.status == "succeeded"
        assert "provider_response" not in result.contract.model_dump(mode="json")
        assert "raw" not in result.contract.model_dump(mode="json")


def test_multistep_response_collects_capability_contracts() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找 500 元以内的白鞋，再比较价格")
    )

    contracts = state.response.data["contracts"]

    assert [contract["capability"] for contract in contracts] == ["shopping_search"]
    assert all(contract["status"] == "succeeded" for contract in contracts)


def test_failed_capability_contract_uses_stable_errors() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="哪个便宜")
    )

    result = state.tool_results[0]

    assert result.success is False
    assert result.contract is not None
    assert result.contract.capability == "shopping_search"
    assert result.contract.status == "failed"
    assert result.contract.errors
    assert result.contract.errors[0].code
    assert result.contract.errors[0].message

from datetime import datetime, timezone

from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.capability_output import build_capability_output_contract
from multimodal_agent.schemas.memory import MemoryQuery, memory_item_from_capability_contract


def test_memory_save_can_store_capability_contract_summary() -> None:
    contract = build_capability_output_contract(
        capability="render_3d",
        status="succeeded",
        output_ref="mock://render/chair-1",
        data={"summary": "生成了椅子的 3D 渲染预览。", "provider_response": {"secret": "removed"}},
        metadata={"provider": "mock"},
    )
    item = memory_item_from_capability_contract(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        contract=contract,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store = InMemoryStore()

    store.save(item)
    result = store.search(MemoryQuery(user_id="u1", query="椅子 渲染", top_k=3))

    assert [memory.memory_id for memory in result.items] == ["m1"]
    assert result.items[0].artifact_refs == ["mock://render/chair-1"]
    assert result.memory_context
    assert "provider_response" not in result.items[0].model_dump_json()

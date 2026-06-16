from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from multimodal_agent.schemas.capability_output import build_capability_output_contract
from multimodal_agent.schemas.memory import MemoryItem, memory_item_from_capability_contract


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_memory_item_supports_artifact_refs_without_raw_media() -> None:
    item = MemoryItem(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        memory_type="image",
        summary="用户生成过一张白色运动鞋海报。",
        content={"capability": "image_generation", "output_ref": "mock://image/poster-1"},
        tags=["image_generation", "poster"],
        source="capability_output",
        artifact_refs=["mock://image/poster-1"],
        created_at=NOW,
    )

    assert item.artifact_refs == ["mock://image/poster-1"]
    assert item.content["output_ref"] == "mock://image/poster-1"


@pytest.mark.parametrize(
    "content",
    [
        {"api_key": "sk-test"},
        {"Authorization": "Bearer secret"},
        {"image_base64": "abc"},
        {"media": "data:image/png;base64,abc"},
        {"provider_response": {"raw": "payload"}},
    ],
)
def test_memory_item_rejects_sensitive_or_raw_payloads(content: dict) -> None:
    with pytest.raises(ValidationError):
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            memory_type="artifact",
            summary="unsafe memory",
            content=content,
            created_at=NOW,
        )


def test_memory_item_from_capability_contract_keeps_safe_summary_and_refs() -> None:
    contract = build_capability_output_contract(
        capability="image_generation",
        status="succeeded",
        output_ref="mock://image/poster-1",
        data={"summary": "生成了白色运动鞋海报。", "raw": "removed"},
        metadata={"provider": "mock"},
    )

    item = memory_item_from_capability_contract(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        contract=contract,
        created_at=NOW,
    )

    assert item.memory_type == "image"
    assert item.summary == "生成了白色运动鞋海报。"
    assert item.artifact_refs == ["mock://image/poster-1"]
    assert item.content == {
        "capability": "image_generation",
        "status": "succeeded",
        "output_ref": "mock://image/poster-1",
        "summary": "生成了白色运动鞋海报。",
    }

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.schemas.generation import ImageGenerationResult, RenderResult
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.perception import PerceptionBundle, VisualUnderstandingResult
from assistant_agent.schemas.planning import IntentResult, TaskPlan, TaskStep
from assistant_agent.schemas.products import PriceCompareResult, ProductResult
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolCallRecord, ToolResult, ToolSelection


def test_user_request_serializes_and_deserializes() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="帮我找相似的鞋",
        image_ids=["img1"],
    )

    payload = request.model_dump_json()
    restored = UserRequest.model_validate_json(payload)

    assert restored == request
    assert restored.image_ids == ["img1"]
    assert restored.video_ids == []


def test_user_request_requires_user_and_session() -> None:
    with pytest.raises(ValidationError):
        UserRequest(user_id="", session_id="s1")

    with pytest.raises(ValidationError):
        UserRequest(user_id="u1", session_id="")


def test_agent_response_requires_message() -> None:
    response = AgentResponse(message="已找到相似商品", output_refs=["product:p1"])

    assert response.message == "已找到相似商品"
    assert response.output_refs == ["product:p1"]

    with pytest.raises(ValidationError):
        AgentResponse(message="")


def test_perception_bundle_contains_visual_understanding() -> None:
    visual = VisualUnderstandingResult(
        objects=["白色低帮运动鞋"],
        colors=["白色"],
        materials=["皮革", "橡胶"],
        scene="室内桌面展示",
        style_tags=["简约", "日系"],
        summary="视频中展示了一双白色低帮运动鞋。",
    )

    bundle = PerceptionBundle(visual=visual, asr_text="帮我找同款")

    assert bundle.visual is not None
    assert bundle.visual.objects == ["白色低帮运动鞋"]
    assert bundle.ocr_text == []


def test_intent_result_validates_intent_and_confidence() -> None:
    intent = IntentResult(
        intent="search_product",
        confidence=0.91,
        rationale="用户明确要求找相似商品",
    )

    assert intent.intent == "search_product"

    with pytest.raises(ValidationError):
        IntentResult(intent="unknown", confidence=0.5, rationale="bad")

    with pytest.raises(ValidationError):
        IntentResult(intent="chat", confidence=1.5, rationale="bad")


def test_task_plan_serializes_nested_steps() -> None:
    plan = TaskPlan(
        goal="搜索并比较白色低帮运动鞋",
        steps=[
            TaskStep(step_id="s1", action="search", tool_name="product_search"),
            TaskStep(
                step_id="s2",
                action="compare",
                tool_name="price_compare",
                depends_on=["s1"],
            ),
        ],
    )

    restored = TaskPlan.model_validate_json(plan.model_dump_json())

    assert restored.steps[1].depends_on == ["s1"]
    assert restored.requires_followup is False


def test_tool_models_capture_selection_result_and_call_record() -> None:
    selected = ToolSelection(
        tool_name="product_search",
        reason="需要搜索商品候选",
        input={"query": "白色低帮运动鞋"},
        step_id="s1",
    )
    result = ToolResult(
        tool_name="product_search",
        success=True,
        data={"count": 3},
        latency_ms=12,
    )
    record = ToolCallRecord(
        call_id="c1",
        tool_name=selected.tool_name,
        input=selected.input,
        status="succeeded",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        output_ref="tool-result:c1",
    )

    assert result.success is True
    assert record.status == "succeeded"

    with pytest.raises(ValidationError):
        ToolResult(tool_name="product_search", success=True, latency_ms=-1)


def test_product_and_price_compare_models_validate_ranges() -> None:
    product = ProductResult(
        product_id="p1",
        title="白色低帮运动鞋",
        price=299.0,
        platform="mock-shop",
        similarity=0.88,
        rating=4.7,
    )
    comparison = PriceCompareResult(
        query="白色低帮运动鞋",
        items=[product],
        best_value_product_id="p1",
        summary="p1 价格最低且相似度较高",
    )

    restored = PriceCompareResult.model_validate_json(comparison.model_dump_json())

    assert restored.items[0].product_id == "p1"

    with pytest.raises(ValidationError):
        ProductResult(
            product_id="p2",
            title="bad",
            price=-1,
            platform="mock-shop",
        )


def test_generation_and_render_results_validate_status() -> None:
    image = ImageGenerationResult(
        task_id="img-task-1",
        status="succeeded",
        image_url="local://image.png",
        prompt="日系海报风格",
    )
    render = RenderResult(
        task_id="render-task-1",
        status="pending",
        preview_url="local://preview.png",
    )

    assert image.status == "succeeded"
    assert render.status == "pending"

    with pytest.raises(ValidationError):
        ImageGenerationResult(task_id="bad", status="done", prompt="x")


def test_memory_item_serializes_and_validates_relevance() -> None:
    item = MemoryItem(
        memory_id="m1",
        user_id="u1",
        memory_type="preference",
        content={"style": "日系"},
        summary="用户偏好日系风格",
        relevance=0.8,
        reason="风格要求匹配",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    restored = MemoryItem.model_validate_json(item.model_dump_json())

    assert restored.memory_type == "preference"
    assert restored.content["style"] == "日系"

    with pytest.raises(ValidationError):
        MemoryItem(
            memory_id="m2",
            user_id="u1",
            memory_type="preference",
            summary="bad",
            relevance=1.1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

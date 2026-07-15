from assistant_agent.services.image_generation_adapter import (
    ImageGenerationInput,
    MockImageGenerationAdapter,
    build_image_prompt,
)
from assistant_agent.tools.image_generation_tool import ImageGenerationTool


def test_build_prompt_contains_product_and_japanese_poster_style() -> None:
    prompt = build_image_prompt(
        ImageGenerationInput(
            prompt="日系海报",
            product_title="白色低帮运动鞋",
        )
    )

    assert "白色低帮运动鞋" in prompt
    assert "日系海报" in prompt
    assert "商品主体" in prompt


def test_image_generation_tool_returns_tool_result() -> None:
    result = ImageGenerationTool(adapter=MockImageGenerationAdapter()).run(
        {
            "style": "日系海报",
            "product_title": "白色低帮运动鞋",
        }
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["task_id"] == "mock_image_task_1"
    assert result.data["status"] == "succeeded"
    assert "白色低帮运动鞋" in result.data["prompt"]
    assert "日系海报" in result.data["prompt"]
    assert result.output_ref == "local://generated/poster.png"


def test_image_generation_tool_accepts_product_id_without_prompt() -> None:
    result = ImageGenerationTool().run({"product_id": "p1"})

    assert result.success is True
    assert result.data is not None
    assert "p1" in result.data["prompt"]
    assert "日系海报" in result.data["prompt"]


def test_image_generation_tool_returns_structured_error_without_prompt_or_product() -> None:
    result = ImageGenerationTool().run({})

    assert result.success is False
    assert result.error == "缺少生成 prompt 或商品信息，无法生成图片"

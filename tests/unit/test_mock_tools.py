from multimodal_agent.tools.image_generation_tool import ImageGenerationTool
from multimodal_agent.tools.memory_tool import MemoryTool
from multimodal_agent.tools.price_compare_tool import PriceCompareTool
from multimodal_agent.tools.product_search_tool import ProductSearchTool
from multimodal_agent.tools.render_tool import Render3DTool
from multimodal_agent.tools.vision_tool import VisionUnderstandingTool


def test_vision_tool_returns_stable_visual_result() -> None:
    result = VisionUnderstandingTool().run({"video_ids": ["v1"], "question": "视频里有什么"})

    assert result.success is True
    assert result.data is not None
    assert result.data["objects"] == ["白色低帮运动鞋"]
    assert result.output_ref == "mock://vision/white-low-top-sneaker"


def test_vision_tool_fails_without_media() -> None:
    result = VisionUnderstandingTool().run({"question": "图里是什么"})

    assert result.success is False
    assert result.error == "缺少图片或视频 ID，无法进行视觉理解"


def test_product_search_returns_three_mock_products() -> None:
    result = ProductSearchTool().run({"query": "白色低帮运动鞋"})

    assert result.success is True
    assert result.data is not None
    assert len(result.data["items"]) == 3
    assert result.data["items"][0]["product_id"] == "p1"


def test_product_search_fails_without_description() -> None:
    result = ProductSearchTool().run({})

    assert result.success is False
    assert result.error == "缺少商品描述，无法搜索"


def test_price_compare_sorts_by_price() -> None:
    search_result = ProductSearchTool().run({"query": "白色低帮运动鞋"})
    assert search_result.data is not None

    result = PriceCompareTool().run({"items": search_result.data["items"]})

    assert result.success is True
    assert result.data is not None
    assert [item["product_id"] for item in result.data["items"]] == ["p2", "p1", "p3"]
    assert result.data["best_value_product_id"] == "p2"


def test_price_compare_fails_without_items() -> None:
    result = PriceCompareTool().run({"items": []})

    assert result.success is False
    assert result.error == "缺少商品候选列表，无法比价"


def test_image_generation_returns_local_url() -> None:
    result = ImageGenerationTool().run({"prompt": "生成日系海报"})

    assert result.success is True
    assert result.data is not None
    assert result.data["status"] == "succeeded"
    assert result.output_ref == "local://generated/poster.png"


def test_image_generation_fails_without_prompt_or_product() -> None:
    result = ImageGenerationTool().run({})

    assert result.success is False
    assert result.error == "缺少生成 prompt 或商品信息，无法生成图片"


def test_render_tool_returns_preview_and_model_urls() -> None:
    result = Render3DTool().run({"product_id": "p1", "scene": "客厅"})

    assert result.success is True
    assert result.data is not None
    assert result.data["preview_url"] == "local://render/preview.png"
    assert result.data["model_url"] == "local://render/model.glb"


def test_render_tool_fails_without_product_or_image() -> None:
    result = Render3DTool().run({"scene": "客厅"})

    assert result.success is False
    assert result.error == "缺少商品或图片输入，无法渲染"


def test_memory_tool_retrieves_and_saves_stable_items() -> None:
    retrieve = MemoryTool().run(
        {"action": "retrieve", "user_id": "u1", "query": "上次那个黑色包"}
    )
    save = MemoryTool().run(
        {"action": "save", "user_id": "u1", "content": {"style": "日系"}}
    )

    assert retrieve.success is True
    assert retrieve.data is not None
    assert retrieve.data["items"][0]["memory_id"] == "m1"
    assert save.success is True
    assert save.data is not None
    assert save.data["memory_id"] == "m_saved_1"


def test_memory_tool_returns_structured_errors() -> None:
    retrieve = MemoryTool().run({"action": "retrieve", "user_id": "u1"})
    save = MemoryTool().run({"action": "save", "user_id": "u1"})

    assert retrieve.success is False
    assert retrieve.error == "缺少检索 query，无法检索记忆"
    assert save.success is False
    assert save.error == "缺少保存内容，无法写入记忆"

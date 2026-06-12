from multimodal_agent.schemas.generation import RenderResult
from multimodal_agent.services.render_adapter import MockRenderAdapter, RenderInput
from multimodal_agent.tools.render_tool import Render3DTool


def test_mock_render_adapter_creates_living_room_render_task() -> None:
    adapter = MockRenderAdapter()

    result = adapter.create_render(
        RenderInput(
            product_id="p1",
            scene="客厅",
            material="皮革和橡胶",
            lighting="自然光",
            camera="正面 45 度",
        )
    )

    assert isinstance(result, RenderResult)
    assert result.task_id == "mock_render_task_1"
    assert result.status == "succeeded"
    assert result.preview_url == "local://render/preview.png"


def test_render_tool_returns_preview_url_and_task_status() -> None:
    result = Render3DTool(adapter=MockRenderAdapter()).run(
        {
            "product_id": "p1",
            "scene": "客厅",
            "lighting": "自然光",
            "camera": "正面 45 度",
        }
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["task_id"] == "mock_render_task_1"
    assert result.data["status"] == "succeeded"
    assert result.data["preview_url"] == "local://render/preview.png"
    assert result.output_ref == "local://render/preview.png"


def test_render_tool_supports_image_url_input() -> None:
    result = Render3DTool().run({"image_url": "local://product.png", "scene": "客厅"})

    assert result.success is True
    assert result.data is not None
    assert result.data["status"] == "succeeded"


def test_render_tool_returns_structured_error_without_product_or_image() -> None:
    result = Render3DTool().run({"scene": "客厅"})

    assert result.success is False
    assert result.error == "缺少商品或图片输入，无法渲染"


def test_mock_render_adapter_requires_product_or_image() -> None:
    adapter = MockRenderAdapter()

    try:
        adapter.create_render(RenderInput(scene="客厅"))
    except ValueError as exc:
        assert str(exc) == "缺少商品或图片输入，无法渲染"
    else:
        raise AssertionError("expected ValueError")

from assistant_agent.schemas.generation import RenderResult
from assistant_agent.services.render_adapter import MockRenderAdapter, RenderInput, RenderRequest
from assistant_agent.tools.render_tool import Render3DTool


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
    assert result.render_id == "mock_render_task_1"
    assert result.status == "succeeded"
    assert result.provider == "mock"
    assert result.preview_url == "mock://render/preview.png"
    assert result.output_ref == "mock://render/preview.png"


def test_mock_render_adapter_supports_text_only_scene() -> None:
    adapter = MockRenderAdapter()

    result = adapter.render(RenderRequest(scene_description="北欧风客厅，浅灰色沙发"))

    assert result.status == "succeeded"
    assert result.output_ref == "mock://render/preview.png"
    assert result.scene_description == "北欧风客厅，浅灰色沙发"
    assert result.used_inputs["scene_description"] == "北欧风客厅，浅灰色沙发"


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
    assert result.data["preview_url"] == "mock://render/preview.png"
    assert result.data["model_url"] == "mock://render/model.glb"
    assert result.output_ref == "mock://render/preview.png"


def test_render_tool_supports_text_only_scene() -> None:
    result = Render3DTool().run({"scene": "客厅"})

    assert result.success is True
    assert result.output_ref == "mock://render/preview.png"


def test_render_tool_supports_image_url_input() -> None:
    result = Render3DTool().run({"image_url": "local://product.png", "scene": "客厅"})

    assert result.success is True
    assert result.data is not None
    assert result.data["status"] == "succeeded"


def test_render_tool_returns_structured_error_without_scene() -> None:
    result = Render3DTool().run({"product_id": "p1"})

    assert result.success is False
    assert result.data is not None
    assert result.data["errors"][0]["code"] == "render_missing_scene"
    assert result.error == "render_missing_scene: Render request requires scene_description or scene."

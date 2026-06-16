from multimodal_agent.schemas.generation import RenderResult
from multimodal_agent.services.render_adapter import MockRenderAdapter, RenderRequest
from multimodal_agent.tools.render_tool import Render3DTool


def test_mock_render_adapter_returns_render_schema() -> None:
    result = MockRenderAdapter().render(
        RenderRequest(product_ref="p1", scene_description="客厅")
    )

    assert isinstance(result, RenderResult)
    assert result.status == "succeeded"
    assert result.provider == "mock"
    assert result.output_ref == "mock://render/preview.png"
    assert result.preview_url == "mock://render/preview.png"
    assert result.model_url == "mock://render/model.glb"
    assert result.used_inputs["product_ref"] == "p1"


def test_mock_render_adapter_rejects_missing_scene() -> None:
    result = MockRenderAdapter().render(RenderRequest(product_ref="p1"))

    assert result.status == "failed"
    assert result.errors[0]["code"] == "render_missing_scene"


def test_render_tool_returns_structured_error_without_provider_details() -> None:
    result = Render3DTool(adapter=MockRenderAdapter()).run({"product_ref": "p1"})

    assert result.success is False
    assert result.tool_name == "render_3d"
    assert result.error
    assert result.data is not None
    assert result.data["errors"][0]["code"] == "render_missing_scene"

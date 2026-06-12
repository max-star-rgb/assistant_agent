import pytest

from multimodal_agent.schemas.generation import RenderResult
from multimodal_agent.services.render_adapter import MockRenderAdapter, RenderInput
from multimodal_agent.tools.render_tool import Render3DTool


def test_mock_render_adapter_returns_render_schema() -> None:
    result = MockRenderAdapter().create_render(
        RenderInput(product_id="p1", scene="客厅")
    )

    assert isinstance(result, RenderResult)
    assert result.status == "succeeded"
    assert result.preview_url == "local://render/preview.png"
    assert result.model_url == "local://render/model.glb"


def test_mock_render_adapter_rejects_missing_product_or_image() -> None:
    with pytest.raises(ValueError, match="缺少商品或图片输入"):
        MockRenderAdapter().create_render(RenderInput(scene="客厅"))


def test_render_tool_returns_structured_error_without_provider_details() -> None:
    result = Render3DTool(adapter=MockRenderAdapter()).run({"scene": "客厅"})

    assert result.success is False
    assert result.tool_name == "render_3d"
    assert result.error

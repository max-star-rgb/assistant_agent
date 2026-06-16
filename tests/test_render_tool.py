from multimodal_agent.services.render_adapter import MockRenderAdapter
from multimodal_agent.tools.render_tool import Render3DTool


def test_render_tool_supports_text_only_request() -> None:
    result = Render3DTool(adapter=MockRenderAdapter()).run(
        {"scene_description": "北欧风客厅，浅灰色布艺沙发", "style": "写实"}
    )

    assert result.success is True
    assert result.output_ref == "mock://render/preview.png"
    assert result.data is not None
    assert result.data["provider"] == "mock"
    assert result.data["scene_description"] == "北欧风客厅，浅灰色布艺沙发"
    assert result.data["used_inputs"]["style"] == "写实"


def test_render_tool_reports_render_missing_scene() -> None:
    result = Render3DTool(adapter=MockRenderAdapter()).run({"product_ref": "p1"})

    assert result.success is False
    assert result.output_ref is None
    assert result.data is not None
    assert result.data["errors"][0]["code"] == "render_missing_scene"

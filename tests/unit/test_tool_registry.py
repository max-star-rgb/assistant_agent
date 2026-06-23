import pytest

from multimodal_agent.schemas.tools import ToolResult, ToolSpec
from multimodal_agent.tools.product_search_tool import ProductSearchTool
from multimodal_agent.tools.registry import ToolRegistry, create_default_registry


def test_register_get_and_list_tool() -> None:
    registry = ToolRegistry()
    tool = ProductSearchTool()

    registry.register(tool)

    assert registry.get("product_search") is tool
    assert registry.list() == ["product_search"]


def test_duplicate_registration_fails() -> None:
    registry = ToolRegistry()
    registry.register(ProductSearchTool())

    with pytest.raises(ValueError, match="Tool already registered"):
        registry.register(ProductSearchTool())


def test_get_missing_tool_fails() -> None:
    registry = ToolRegistry()

    with pytest.raises(KeyError, match="Tool not registered"):
        registry.get("missing")


def test_default_registry_contains_mock_tools() -> None:
    registry = create_default_registry()

    assert set(registry.list()) >= {
        "vision_understanding",
        "product_search",
        "price_compare",
        "image_generation",
        "render_3d",
        "memory",
    }


def test_registry_list_specs_is_the_canonical_tool_description() -> None:
    registry = create_default_registry()

    specs = registry.list_specs()
    descriptions = registry.describe_tools()

    assert specs
    assert all(isinstance(spec, ToolSpec) for spec in specs)
    assert descriptions == [spec.model_dump(mode="json") for spec in specs]

    video = next(spec for spec in specs if spec.name == "video_understanding")
    assert video.input_schema["fields"]
    assert "video_ids" in " ".join(video.runtime_constraints)
    assert video.when_to_use
    assert "api_key" not in str(video.model_dump(mode="json")).lower()


def test_registry_run_returns_tool_result() -> None:
    registry = create_default_registry()

    result = registry.run("product_search", {"query": "白色低帮运动鞋"})

    assert isinstance(result, ToolResult)
    assert result.tool_name == "product_search"
    assert result.success is True
    assert result.data is not None

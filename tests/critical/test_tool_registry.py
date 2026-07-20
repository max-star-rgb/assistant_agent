import pytest

from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.services.agent_communication import AgentCommunicationService
from assistant_agent.tools.registry import ToolRegistry, create_default_registry
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool


def test_register_get_and_list_tool() -> None:
    registry = ToolRegistry()
    tool = ShoppingSearchTool()

    registry.register(tool)

    assert registry.get("shopping_search") is tool
    assert registry.list() == ["shopping_search"]


def test_duplicate_registration_fails() -> None:
    registry = ToolRegistry()
    registry.register(ShoppingSearchTool())

    with pytest.raises(ValueError, match="Tool already registered"):
        registry.register(ShoppingSearchTool())


def test_get_missing_tool_fails() -> None:
    registry = ToolRegistry()

    with pytest.raises(KeyError, match="Tool not registered"):
        registry.get("missing")


def test_get_spec_uses_the_same_contract_as_list_specs() -> None:
    registry = create_default_registry()

    listed = {spec.name: spec for spec in registry.list_specs()}

    assert registry.get_spec("shopping_search") == listed["shopping_search"]


def test_get_spec_missing_tool_fails() -> None:
    registry = ToolRegistry()

    with pytest.raises(KeyError, match="Tool not registered"):
        registry.get_spec("missing")


def test_default_registry_contains_mock_tools() -> None:
    registry = create_default_registry()

    assert set(registry.list()) >= {
        "vision_understanding",
        "shopping_search",
        "image_generation",
        "render_3d",
        "memory",
    }
    assert "product_search" not in registry.list()
    assert "price_compare" not in registry.list()


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
    assert video.side_effect.level == "external_read"
    assert video.side_effect.requires_confirmation is False
    assert "api_key" not in str(video.model_dump(mode="json")).lower()

    image = next(spec for spec in specs if spec.name == "image_generation")
    assert image.side_effect.level == "compensatable"
    assert image.side_effect.compensation_hint
    assert image.execution.dependency_mode == "terminal"
    assert image.execution.realtime_safety == "needs_progress"
    assert image.execution.artifact_reuse == "requires_validation"
    assert image.execution.progress_message == "我开始生成，可能需要一点时间。"

    memory_save = next(spec for spec in specs if spec.name == "memory_save")
    assert memory_save.side_effect.level == "pending_confirmation"
    assert memory_save.side_effect.requires_confirmation is True
    assert memory_save.execution.realtime_safety == "needs_confirmation"
    assert memory_save.execution.resource_writes == ["memory"]
    assert memory_save.execution.artifact_reuse == "do_not_reuse"

    assert all(spec.name != "product_search" for spec in specs)

    shopping_search = next(spec for spec in specs if spec.name == "shopping_search")
    assert shopping_search.execution.dependency_mode == "independent"
    assert shopping_search.execution.realtime_safety == "safe"
    assert shopping_search.execution.resource_reads == ["product_catalog", "offers"]
    assert shopping_search.execution.artifact_reuse == "reusable"
    assert shopping_search.execution.progress_message == "我查一下并比一下价格。"

    assert all(spec.name != "price_compare" for spec in specs)

    memory_ingest_status = next(spec for spec in specs if spec.name == "memory_ingest_status")
    assert memory_ingest_status.execution.dependency_mode == "independent"
    assert memory_ingest_status.execution.realtime_safety == "safe"


def test_unknown_tool_execution_policy_is_conservative() -> None:
    spec = ToolSpec(name="custom_notification")

    assert spec.execution.dependency_mode == "requires_prior_observation"
    assert spec.execution.realtime_safety == "needs_confirmation"
    assert spec.execution.artifact_reuse == "requires_validation"
    assert spec.execution.progress_message is None
    assert spec.execution.resource_reads == []
    assert spec.execution.resource_writes == []


def test_opt_in_delegate_tool_uses_terminal_execution_policy() -> None:
    registry = create_default_registry(
        enable_agent_delegation=True,
        agent_communication_service=AgentCommunicationService(),
    )
    spec = next(spec for spec in registry.list_specs() if spec.name == "delegate_to_agent")

    assert spec.execution.dependency_mode == "terminal"
    assert spec.execution.realtime_safety == "needs_progress"


def test_registry_run_returns_tool_result() -> None:
    registry = create_default_registry()

    result = registry.run("shopping_search", {"query": "白色低帮运动鞋"})

    assert isinstance(result, ToolResult)
    assert result.tool_name == "shopping_search"
    assert result.success is True
    assert result.data is not None

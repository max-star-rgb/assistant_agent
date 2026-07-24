"""Stable startup-pluggability and immutable Registry contracts."""

from types import ModuleType
import sys

import pytest
from pydantic import BaseModel

from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.services.agent_service_entry import agent_service_tool_visibility
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.plugins.assembly import ToolPluginAssemblyError
from assistant_agent.tools.plugins.contracts import ToolPluginDescriptor
from assistant_agent.tools.registry import create_default_registry
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig


class _EmptyInput(BaseModel):
    pass


class _ConfiguredReadTool(ToolBase):
    name = "configured_read"
    description = "Read data from an explicitly configured plugin."
    input_schema = _EmptyInput
    output_schema = ToolResult
    category = "read"
    requires_confirmation = False

    def _run(self, input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"configured": True})


class _ConfiguredWriteTool(_ConfiguredReadTool):
    name = "configured_write"
    category = "write"


class _ConfiguredPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="tests.configured", plugin_version="1.0")

    def build_tools(self, context):
        return [_ConfiguredReadTool(), _ConfiguredWriteTool()]


def _install_plugin_module(monkeypatch: pytest.MonkeyPatch, name: str, plugin: object) -> None:
    module = ModuleType(name)
    module.__assistant_tool_plugin__ = plugin
    monkeypatch.setitem(sys.modules, name, module)


def test_configured_plugin_is_owned_and_default_registry_is_sealed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "tests_fake_configured_plugin"
    _install_plugin_module(monkeypatch, module_name, _ConfiguredPlugin())

    first = create_default_registry(plugin_modules=[module_name])
    second = create_default_registry(plugin_modules=[module_name])

    record = first.registration_record("configured_read")
    assert first.sealed is True
    assert first.generation == second.generation
    assert record.plugin_id == "tests.configured"
    assert record.plugin_version == "1.0"
    assert record.source_type == "configured_module"
    assert "plugin_id" not in first.get_spec("configured_read").model_dump(mode="json")
    with pytest.raises(RuntimeError, match="sealed"):
        first.register(_ConfiguredReadTool())


def test_configured_write_tool_is_not_host_enabled_by_plugin_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "tests_fake_write_plugin"
    _install_plugin_module(monkeypatch, module_name, _ConfiguredPlugin())
    registry = create_default_registry(plugin_modules=[module_name])

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u", session_id="s", text="write"),
        registry.list_specs(),
        registry_generation=registry.generation,
        host_configured_tool_names=registry.host_configured_tool_names(),
    )

    assert "configured_read" in selection.run_tool_catalog.available_tool_names
    assert "configured_write" not in selection.run_tool_catalog.available_tool_names
    assert selection.summary.registry_generation == registry.generation


def test_agent_service_entry_profile_exposes_registered_read_tools_by_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "tests_fake_agent_service_read_plugin"
    _install_plugin_module(monkeypatch, module_name, _ConfiguredPlugin())
    registry = create_default_registry(plugin_modules=[module_name])
    request = UserRequest(
        user_id="agent-service-user",
        session_id="agent-service-session",
        text="帮我处理这个请求",
        metadata={"tool_visibility": agent_service_tool_visibility()},
    )

    selection = select_prompt_tool_specs(
        request,
        registry.list_specs(),
        registry_generation=registry.generation,
        host_configured_tool_names=registry.host_configured_tool_names(),
    )

    assert {
        "calendar_search",
        "contacts_search",
        "shopping_search",
        "tool_search",
        "weather",
        "web_fetch",
        "web_search",
    }.issubset(selection.run_tool_catalog.available_tool_names)
    assert "configured_read" in selection.run_tool_catalog.available_tool_names
    assert "configured_write" not in selection.run_tool_catalog.available_tool_names
    assert "calendar_create" not in selection.run_tool_catalog.available_tool_names
    assert "entry_profile_not_allowed" not in {
        reason
        for reasons in selection.run_tool_catalog.excluded_reasons.values()
        for reason in reasons
    }
    assert selection.run_tool_catalog.excluded_reasons["vision_understanding"] == [
        "required_media_not_available"
    ]
    assert "entry_profile:agent_service" in selection.run_tool_catalog.selection_reasons

    video_selection = select_prompt_tool_specs(
        request.model_copy(update={"video_ids": ["live-video-1"]}),
        registry.list_specs(),
        registry_generation=registry.generation,
        host_configured_tool_names=registry.host_configured_tool_names(),
    )
    assert "vision_understanding" in (
        video_selection.run_tool_catalog.available_tool_names
    )


def test_explicit_invalid_plugin_configuration_fails_closed() -> None:
    with pytest.raises(ToolPluginAssemblyError) as exc_info:
        create_default_registry(plugin_modules=["not-a-valid-module"])

    assert exc_info.value.report.issues[0].code == "invalid_module_name"
    assert exc_info.value.report.registrations == []


def test_durable_runtime_finishes_two_phase_assembly_before_seal(tmp_path) -> None:
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            durable_tasks_enabled=True,
            durable_task_path=str(tmp_path / "tasks.sqlite3"),
            langgraph_checkpointer_backend="none",
        )
    )

    assert runtime.registry.sealed is True
    assert "task_plan_submit" in runtime.registry.list()
    assert runtime.durable_task_service is not None
    assert runtime.durable_task_service.registry is runtime.registry

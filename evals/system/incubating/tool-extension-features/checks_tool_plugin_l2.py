"""Stable startup-pluggability and immutable Registry contracts."""

import importlib.util
from types import ModuleType
import sys

import pytest
from pydantic import BaseModel

from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolResult
from assistant_agent.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.media.agent_service_entry import agent_service_tool_visibility
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.plugins.assembly import ToolPluginAssemblyError
from assistant_agent.tools.plugins.contracts import ToolPluginDescriptor
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig


def test_tool_plugin_contract_has_one_canonical_namespace() -> None:
    assert importlib.util.find_spec("assistant_agent.tool_plugins") is None
    assert ToolPluginDescriptor.__module__ == "assistant_agent.tools.plugins.contracts"


class _EmptyInput(BaseModel):
    pass


class _ConfiguredReadTool(ToolBase):
    name = "configured_read"
    description = "Read data from an explicitly configured plugin."
    input_schema = _EmptyInput
    output_schema = ToolResult
    category = "read"

    def _run(self, input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"configured": True})


class _ConfiguredWriteTool(_ConfiguredReadTool):
    name = "configured_write"
    category = "write"


class _ConfiguredGenerateTool(_ConfiguredReadTool):
    name = "configured_generate"
    category = "generate"


class _ConfiguredDangerousTool(_ConfiguredReadTool):
    name = "configured_dangerous"
    category = "dangerous"


class _ConfiguredPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="tests.configured", plugin_version="1.0")

    def build_tools(self, context):
        return [
            _ConfiguredReadTool(),
            _ConfiguredGenerateTool(),
            _ConfiguredWriteTool(),
            _ConfiguredDangerousTool(),
        ]


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


def test_registered_tools_are_exposed_without_category_or_default_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "tests_fake_write_plugin"
    _install_plugin_module(monkeypatch, module_name, _ConfiguredPlugin())
    registry = create_default_registry(plugin_modules=[module_name])

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u", session_id="s", text="write"),
        registry.list_specs(),
        registry_generation=registry.generation,
    )

    assert {
        "configured_read",
        "configured_generate",
        "configured_write",
        "configured_dangerous",
    }.issubset(selection.run_tool_catalog.available_tool_names)
    assert selection.summary.registry_generation == registry.generation


def test_entry_allowed_tools_still_narrows_registered_tools() -> None:
    registry = create_default_registry()
    request = UserRequest(
        user_id="u",
        session_id="s",
        text="manage calendar",
        metadata={
            "tool_visibility": {
                "allowed_tools": ["calendar_search", "calendar_create"],
            }
        },
    )

    selection = select_prompt_tool_specs(
        request,
        registry.list_specs(),
        registry_generation=registry.generation,
    )

    assert {"calendar_search", "calendar_create"}.issubset(
        selection.run_tool_catalog.available_tool_names
    )
    assert set(selection.run_tool_catalog.available_tool_names) == {
        "calendar_search",
        "calendar_create",
    }


def test_agent_service_entry_profile_exposes_all_structurally_eligible_tools(
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
    )

    assert {
        "calendar_search",
        "contacts_search",
    }.issubset(selection.run_tool_catalog.available_tool_names)
    assert {"weather", "web_fetch", "web_search"}.isdisjoint(registry.list())
    assert {"weather", "web_fetch", "web_search"}.isdisjoint(
        selection.run_tool_catalog.available_tool_names
    )
    assert "shopping_search" not in selection.run_tool_catalog.available_tool_names
    assert {
        "configured_read",
        "configured_generate",
        "configured_write",
        "configured_dangerous",
        "calendar_create",
    }.issubset(selection.run_tool_catalog.available_tool_names)
    assert "entry_profile_not_allowed" not in {
        reason
        for reasons in selection.run_tool_catalog.excluded_reasons.values()
        for reason in reasons
    }
    assert selection.run_tool_catalog.excluded_reasons["media_inspect"] == [
        "attached_media_not_available"
    ]
    assert selection.run_tool_catalog.excluded_reasons["live_view_inspect"] == [
        "trusted_live_video_not_available"
    ]
    assert "entry_profile:agent_service" in selection.run_tool_catalog.selection_reasons

    image_selection = select_prompt_tool_specs(
        request.model_copy(update={"image_ids": ["https://example.com/image.jpg"]}),
        registry.list_specs(),
        registry_generation=registry.generation,
    )
    assert "media_inspect" in image_selection.run_tool_catalog.available_tool_names
    assert (
        "live_view_inspect"
        not in image_selection.run_tool_catalog.available_tool_names
    )

    video_selection = select_prompt_tool_specs(
        request.model_copy(
            update={
                "video_ids": ["live-video-1"],
                "metadata": {
                    "tool_visibility": agent_service_tool_visibility(),
                    "transport": "agent_service_websocket",
                    "gateway": {
                        "session_config": {"entry_profile": "agent_service"}
                    },
                },
            }
        ),
        registry.list_specs(),
        registry_generation=registry.generation,
    )
    assert "live_view_inspect" in (
        video_selection.run_tool_catalog.available_tool_names
    )
    assert "media_inspect" not in (
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

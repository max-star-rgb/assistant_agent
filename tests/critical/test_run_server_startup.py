"""Regression coverage for operator-visible server startup diagnostics."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace

from pydantic import BaseModel

from assistant_agent.api import app as api_app
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.server_startup_summary import format_tool_registry_summary
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.plugins.contracts import ToolPluginDescriptor
from assistant_agent.tools.registry import create_default_registry


class _EmptyInput(BaseModel):
    pass


class _StartupVisibleTool(ToolBase):
    name = "startup_visible_tool"
    description = "Tool contributed by a configured startup plugin."
    input_schema = _EmptyInput
    output_schema = ToolResult
    category = "read"
    requires_confirmation = False

    def _run(self, input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True)


class _StartupPlugin:
    descriptor = ToolPluginDescriptor(plugin_id="tests.startup", plugin_version="2.1")

    def build_tools(self, context):
        return [_StartupVisibleTool()]


def test_startup_summary_lists_tools_from_plugin_registry(monkeypatch) -> None:
    module_name = "tests_fake_startup_plugin"
    module = ModuleType(module_name)
    module.__assistant_tool_plugin__ = _StartupPlugin()
    monkeypatch.setitem(sys.modules, module_name, module)
    registry = create_default_registry(
        ProviderConfig(),
        plugin_modules=[module_name],
    )

    output = "\n".join(format_tool_registry_summary(registry))
    assert f"Registered tools ({len(registry.list())}):" in output
    assert "startup_visible_tool (plugin=tests.startup@2.1, source=configured_module)" in output
    assert "Tool registry: sealed=True, generation=sha256:" in output


def test_server_lifespan_prints_the_runtime_registry(monkeypatch, capsys) -> None:
    registry = create_default_registry(ProviderConfig())
    runtime = SimpleNamespace(registry=registry, durable_task_service=None, config=ProviderConfig())
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(api_app.routes_agent, "get_agent_runtime", lambda: runtime)

    worker = asyncio.run(api_app.start_durable_task_worker(app))

    assert worker is None
    output = capsys.readouterr().out
    assert f"Registered tools ({len(registry.list())}):" in output
    assert "web_search (plugin=web@1, source=builtin)" in output

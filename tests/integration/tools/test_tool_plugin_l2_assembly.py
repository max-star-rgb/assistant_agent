"""Focused development checks for L2 Tool plugin assembly failures."""

from types import ModuleType
import sys

import pytest

from assistant_agent.tool_plugins.assembly import (
    ToolPluginAssemblyError,
    configured_plugin_modules_from_env,
)
from assistant_agent.tool_plugins import assembly as plugin_assembly
from assistant_agent.tool_plugins.contracts import ToolPluginDescriptor
from assistant_agent.tools.registry import create_default_registry

from tests.integration.tools.test_tool_plugin_l2 import _ConfiguredReadTool


def _module(monkeypatch: pytest.MonkeyPatch, name: str, plugin: object | None) -> None:
    module = ModuleType(name)
    if plugin is not None:
        module.__assistant_tool_plugin__ = plugin
    monkeypatch.setitem(sys.modules, name, module)


def test_plugin_module_env_parser_trims_deduplicates_and_preserves_order() -> None:
    modules = configured_plugin_modules_from_env(
        {"MULTIMODAL_AGENT_TOOL_PLUGIN_MODULES": " acme.one,acme.two, acme.one ,,"}
    )

    assert modules == ["acme.one", "acme.two"]


def test_unconfigured_module_is_not_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    original_import = plugin_assembly.importlib.import_module

    def recording_import(name: str):
        imported.append(name)
        return original_import(name)

    monkeypatch.setattr(plugin_assembly.importlib, "import_module", recording_import)

    create_default_registry(plugin_modules=[])

    assert imported == []


@pytest.mark.parametrize(
    ("plugin", "expected_code"),
    [
        (None, "missing_plugin_export"),
        (
            type(
                "BadApiPlugin",
                (),
                {
                    "descriptor": {
                        "plugin_id": "tests.bad_api",
                        "plugin_version": "1",
                        "api_version": "tool_plugin_v2",
                    },
                    "build_tools": lambda self, context: [],
                },
            )(),
            "invalid_plugin_descriptor",
        ),
    ],
)
def test_invalid_configured_plugin_protocol_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    plugin: object | None,
    expected_code: str,
) -> None:
    name = "tests_fake_invalid_plugin"
    _module(monkeypatch, name, plugin)

    with pytest.raises(ToolPluginAssemblyError) as exc_info:
        create_default_registry(plugin_modules=[name])

    assert exc_info.value.report.issues[0].code == expected_code


def test_duplicate_builtin_plugin_id_fails_before_registry_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = type(
        "DuplicatePythonExecutionPlugin",
        (),
        {
            "descriptor": ToolPluginDescriptor(
                plugin_id="python_execution",
                plugin_version="external",
            ),
            "build_tools": lambda self, context: [],
        },
    )()
    name = "tests_fake_duplicate_plugin"
    _module(monkeypatch, name, plugin)

    with pytest.raises(ToolPluginAssemblyError) as exc_info:
        create_default_registry(plugin_modules=[name])

    assert any(issue.code == "duplicate_plugin_id" for issue in exc_info.value.report.issues)
    assert exc_info.value.report.registrations == []


def test_duplicate_tool_name_fails_without_partial_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DuplicateTool(_ConfiguredReadTool):
        name = "python_interpreter"

    plugin = type(
        "DuplicateToolPlugin",
        (),
        {
            "descriptor": ToolPluginDescriptor(plugin_id="tests.duplicate_tool", plugin_version="1"),
            "build_tools": lambda self, context: [DuplicateTool()],
        },
    )()
    name = "tests_fake_duplicate_tool_plugin"
    _module(monkeypatch, name, plugin)

    with pytest.raises(ToolPluginAssemblyError) as exc_info:
        create_default_registry(plugin_modules=[name])

    assert any(issue.code == "duplicate_tool_name" for issue in exc_info.value.report.issues)
    assert exc_info.value.report.registrations == []


def test_plugin_build_failure_returns_only_a_sanitized_empty_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_build(self, context):
        raise RuntimeError("build exploded")

    plugin = type(
        "FailingPlugin",
        (),
        {
            "descriptor": ToolPluginDescriptor(plugin_id="tests.failing", plugin_version="1"),
            "build_tools": fail_build,
        },
    )()
    name = "tests_fake_failing_plugin"
    _module(monkeypatch, name, plugin)

    with pytest.raises(ToolPluginAssemblyError) as exc_info:
        create_default_registry(plugin_modules=[name])

    assert exc_info.value.report.issues[0].code == "plugin_build_failed"
    assert exc_info.value.report.registrations == []

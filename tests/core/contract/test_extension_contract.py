from __future__ import annotations

import sys
from types import ModuleType

import pytest

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.models import RunToolCatalog
from assistant_agent.tools.plugins.assembly import assemble_tool_plugins
from assistant_agent.tools.plugins.contracts import (
    ToolPluginContext,
    ToolPluginDescriptor,
)
from assistant_agent.tools.registry import ToolRegistry
from tests.core.support import ProbeTool, offline_config


class ProbePlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="tests.probe",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext) -> list[ProbeTool]:
        return [ProbeTool()]


@pytest.mark.core_invariant("EXT-001")
def test_probe_plugin_assembles_schema_and_executes_through_governed_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "tests_core_probe_plugin"
    module = ModuleType(module_name)
    module.__assistant_tool_plugin__ = ProbePlugin()
    monkeypatch.setitem(sys.modules, module_name, module)
    assembly = assemble_tool_plugins(
        ToolPluginContext(
            config=offline_config(),
            mcp_server_configs=[],
        ),
        builtin_plugins=(),
        configured_module_names=(module_name,),
    )
    registry = ToolRegistry()
    registry.register_many(assembly.contributions)
    registry.seal(assembly_report=assembly.report)

    spec = registry.get_spec(ProbeTool.name)
    registration = registry.registration_record(ProbeTool.name)
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="request-sentinel",
    )
    state = AgentState.from_request(request)
    state.run_tool_catalog = RunToolCatalog(
        available_tool_names=[ProbeTool.name]
    )
    decision = AssistantToolCall(
        tool_name=ProbeTool.name,
        tool_input={"value": "value-sentinel"},
    )
    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    assert validation.accepted is True
    assert validation.validated_input is not None
    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-sentinel",
        decision.tool_name,
        decision.tool_input,
        validated_input=validation.validated_input,
    )

    assert set(spec.input_schema["properties"]) == {"value"}
    assert spec.input_schema["required"] == ["value"]
    assert registration.plugin_id == "tests.probe"
    assert registration.plugin_version == "1"
    assert registration.source_type == "configured_module"
    assert result.success is True
    assert result.data == {"value": "value-sentinel"}

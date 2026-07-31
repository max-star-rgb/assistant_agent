"""Cross-layer mock runtime acceptance for a tool built by a capability plugin."""

from types import ModuleType
import sys

import pytest
from pydantic import BaseModel

from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.decision_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolResult
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.contracts import ToolPluginDescriptor
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.plugins.registry_factory import create_default_registry


class _ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-plugin-runtime"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class _ConfiguredInput(BaseModel):
    query: str


class _ConfiguredRuntimeTool(ToolBase):
    name = "configured_runtime_read"
    description = "Read from an explicitly configured test plugin."
    input_schema = _ConfiguredInput
    output_schema = ToolResult
    category = "read"

    def _run(self, input: _ConfiguredInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"answer": input.query},
            model_observation={"summary": f"configured:{input.query}"},
        )


class _ConfiguredRuntimePlugin:
    descriptor = ToolPluginDescriptor(
        plugin_id="tests.runtime",
        plugin_version="1",
    )

    def build_tools(self, context: ToolPluginContext):
        return [_ConfiguredRuntimeTool()]


def test_configured_plugin_tool_runs_through_default_runtime_governance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "tests_fake_runtime_plugin"
    module = ModuleType(module_name)
    module.__assistant_tool_plugin__ = _ConfiguredRuntimePlugin()
    monkeypatch.setitem(sys.modules, module_name, module)
    config = ProviderConfig(langgraph_checkpointer_backend="none")
    registry = create_default_registry(config, plugin_modules=[module_name])
    adapter = _ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-plugin-runtime",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="configured-call-1",
                        name="configured_runtime_read",
                        arguments={"query": "hello"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-plugin-runtime",
                finish_reason="stop",
                response_text="configured tool completed",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        config=config,
        registry=registry,
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(user_id="plugin-user", session_id="plugin-session", text="read configured")
    )

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls] == ["configured_runtime_read"]
    assert state.tool_results[0].data == {"answer": "hello"}
    assert registry.registration_record("configured_runtime_read").plugin_id == "tests.runtime"

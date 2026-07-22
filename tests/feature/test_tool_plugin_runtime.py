"""Cross-layer mock runtime acceptance for a tool built by a capability plugin."""

from datetime import datetime, timezone
from types import ModuleType
import sys

import pytest
from pydantic import BaseModel

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.contracts import ToolPluginDescriptor
from assistant_agent.tools.plugins.memory.plugin import MemoryToolPlugin
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


class _ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-plugin-runtime"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def test_agent_runtime_executes_tool_built_by_plugin_in_mock_mode() -> None:
    config = ProviderConfig(langgraph_checkpointer_backend="none")
    registry = ToolRegistry()
    for tool in MemoryToolPlugin().build_tools(
        ToolPluginContext(config=config, mcp_server_configs=[])
    ):
        registry.register(tool)
    memory_store = InMemoryStore()
    memory_store.save(
        MemoryItem(
            memory_id="plugin-memory-1",
            user_id="plugin-user",
            memory_type="preference",
            content={"item": "黑色通勤包"},
            summary="用户喜欢黑色通勤包。",
            created_at=datetime.now(timezone.utc),
        )
    )
    adapter = _ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-plugin-runtime",
                finish_reason="tool_calls",
                message_kind="tool_call",
                tool_calls=[
                    NativeToolCall(
                        id="plugin-call-1",
                        name="memory_retrieval",
                        arguments={"query": "通勤包"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-plugin-runtime",
                finish_reason="stop",
                message_kind="final_answer",
                response_text="已通过插件工具读取记忆。",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        config=config,
        registry=registry,
        chat_adapter=adapter,
        memory_store=memory_store,
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(user_id="plugin-user", session_id="plugin-session", text="推荐通勤包")
    )

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls] == ["memory_retrieval"]
    assert state.tool_results[0].success is True
    assert "黑色通勤包" in str(adapter.requests[1].messages)
    assert state.response is not None
    assert state.response.message == "已通过插件工具读取记忆。"


class _ConfiguredInput(BaseModel):
    query: str


class _ConfiguredRuntimeTool(ToolBase):
    name = "configured_runtime_read"
    description = "Read from an explicitly configured test plugin."
    input_schema = _ConfiguredInput
    output_schema = ToolResult
    category = "read"
    requires_confirmation = False

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
                message_kind="tool_call",
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
                message_kind="final_answer",
                response_text="configured tool completed",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        config=config,
        registry=registry,
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(user_id="plugin-user", session_id="plugin-session", text="read configured")
    )

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls] == ["configured_runtime_read"]
    assert state.tool_results[0].data == {"answer": "hello"}
    assert registry.registration_record("configured_runtime_read").plugin_id == "tests.runtime"

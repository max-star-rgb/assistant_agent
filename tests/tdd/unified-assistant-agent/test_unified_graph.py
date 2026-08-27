from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest
from deepagents.backends import FilesystemBackend, LocalShellBackend
from deepagents.middleware import FilesystemMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from assistant_agent.coding.backend import ReadOnlyCodingWorkspaceBackend
from assistant_agent.native_agent import assistant_agent
from assistant_agent.native_agent.assistant_agent import (
    build_assistant_agent,
    build_read_only_worker,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import (
    MockAssistantChatModel,
    read_only_worker_model_view,
)
from assistant_agent.native_agent.state import AssistantAgentState
from assistant_agent.native_agent.tool_profiles import project_tool_profiles


def _tool(name: str, effect: str) -> BaseTool:
    def probe(value: str) -> str:
        """Return one sentinel value."""

        return value

    return StructuredTool.from_function(
        probe,
        name=name,
        metadata={"effect": effect},
    )


def test_main_uses_factory_filesystem_and_unified_hitl(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        assistant_agent,
        "create_deep_agent",
        lambda **kwargs: captured.update(kwargs) or "compiled",
    )
    result = build_assistant_agent(
        MockAssistantChatModel(),
        [
            _tool("read_probe", "read"),
            _tool("write_probe", "write"),
            _tool("dangerous_probe", "dangerous"),
            _tool("generate_probe", "generate"),
            StructuredTool.from_function(
                lambda value: value,
                name="mcp_probe",
                description="Return one MCP sentinel.",
                metadata={"effect": "external", "source": "mcp"},
            ),
        ],
        backend=object(),
        worker_graph=RunnableLambda(lambda state: state),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=(),
        additional_middleware=(
            SimpleNamespace(
                tools=[
                    _tool("start_async_task", "write"),
                    _tool("check_async_task", "read"),
                ]
            ),
        ),
    )

    assert result == "compiled"
    assert not any(
        isinstance(item, FilesystemMiddleware)
        for item in captured["middleware"]
    )
    assert captured["interrupt_on"].keys() >= {
        "write_file",
        "edit_file",
        "delete",
        "execute",
        "write_probe",
        "dangerous_probe",
        "generate_probe",
        "mcp_probe",
        "start_async_task",
    }
    assert "check_async_task" not in captured["interrupt_on"]
    assert sum(
        isinstance(item, TodoListMiddleware) for item in captured["middleware"]
    ) == 1
    assert [item["name"] for item in captured["subagents"]] == [
        "general-purpose"
    ]
    assert captured["name"] == "AssistantAgent"


class _WriteOnceModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        del kwargs
        if any(
            isinstance(message, ToolMessage) and message.name == "write_probe"
            for message in messages
        ):
            return AIMessage(content="write-complete")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_probe",
                    "args": {"value": "sentinel"},
                    "id": "write-call",
                    "type": "tool_call",
                }
            ],
        )


def _compiled_agent(
    tmp_path: Path,
    model: BaseChatModel,
    tools: Sequence[BaseTool] = (),
):
    read_only_backend = ReadOnlyCodingWorkspaceBackend(
        SimpleNamespace(), "repo-sentinel"
    )
    worker = build_read_only_worker(
        read_only_worker_model_view(model),
        tools,
        backend=read_only_backend,
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )
    return build_assistant_agent(
        model,
        tools,
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        worker_graph=worker,
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=(),
    )


def test_simple_request_does_not_require_todo_or_task(tmp_path: Path) -> None:
    graph = _compiled_agent(tmp_path, MockAssistantChatModel())
    result = graph.invoke(
        {"messages": [HumanMessage(content="simple-sentinel")]},
        context=AssistantRunContext(),
        config={"configurable": {"thread_id": "simple-thread"}},
    )
    assert isinstance(result["messages"][-1], AIMessage)
    assert not any(
        isinstance(message, ToolMessage) for message in result["messages"]
    )


@pytest.mark.parametrize("decision,expected", [("approve", 1), ("reject", 0)])
def test_write_interrupts_before_handler_and_resumes_once(
    tmp_path: Path,
    decision: str,
    expected: int,
) -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record one governed write."""

        executed.append(value)
        return "ok"

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        metadata={"effect": "write"},
    )
    assistant = _compiled_agent(tmp_path, _WriteOnceModel(), [tool])
    builder = StateGraph(AssistantAgentState, context_schema=AssistantRunContext)
    builder.add_node("assistant", assistant)
    builder.add_edge(START, "assistant")
    builder.add_edge("assistant", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": f"write-{decision}"}}

    interrupted = graph.invoke(
        {"messages": [HumanMessage(content="write sentinel")]},
        context=AssistantRunContext(),
        config=config,
    )
    assert executed == []
    assert interrupted["__interrupt__"][0].value["action_requests"][0][
        "name"
    ] == "write_probe"

    resumed = graph.invoke(
        Command(resume={"decisions": [{"type": decision}]}),
        context=AssistantRunContext(),
        config=config,
    )
    assert len(executed) == expected
    assert sum(
        isinstance(message, ToolMessage) and message.tool_call_id == "write-call"
        for message in resumed["messages"]
    ) == 1


def test_filesystem_profile_includes_execute() -> None:
    filesystem = next(
        profile
        for profile in project_tool_profiles()
        if profile.profile_id == "filesystem"
    )

    assert filesystem.tool_names[-1] == "execute"

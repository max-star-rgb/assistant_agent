from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest
from deepagents.backends import FilesystemBackend, LocalShellBackend
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr

from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.coding.backend import ReadOnlyCodingWorkspaceBackend
from assistant_agent.native_agent import assistant_agent as assistant_agent_module
from assistant_agent.native_agent.assistant_agent import (
    RecursionFinalSynthesisMiddleware,
    build_assistant_agent,
)
from assistant_agent.native_agent.conditional_tool_exposure import (
    ConditionalToolExposureMiddleware,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.tool_call_limits import PerToolCallLimitMiddleware
from assistant_agent.native_agent.tool_profiles import (
    ToolProfileMiddleware,
    project_tool_profiles,
)


def _tool_names(raw_tools: object) -> set[str]:
    if not isinstance(raw_tools, list):
        return set()
    return {
        function["name"]
        for item in raw_tools
        if isinstance(item, dict)
        and isinstance((function := item.get("function")), dict)
        and isinstance(function.get("name"), str)
    }


def _worker() -> Runnable:
    return RunnableLambda(
        lambda state: {"messages": [AIMessage(content="worker-sentinel")]}
    )


def _agent(
    tmp_path: Path,
    model: BaseChatModel,
    tools: Sequence[BaseTool] = (),
    *,
    worker: Runnable | None = None,
    tool_profiles=(),
):
    return build_assistant_agent(
        model,
        tools,
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        worker_graph=worker or _worker(),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=tool_profiles,
    )


class _CaptureMessagesModel(MockAssistantChatModel):
    observed_messages: list[tuple[Any, ...]] = []

    def _response_message(self, messages, **kwargs):
        self.observed_messages.append(tuple(messages))
        return super()._response_message(messages, **kwargs)


class _FinalSynthesisModel(MockAssistantChatModel):
    _calls: int = PrivateAttr(default=0)
    _tool_choices: list[object] = PrivateAttr(default_factory=list)

    @property
    def tool_choices(self) -> tuple[object, ...]:
        return tuple(self._tool_choices)

    def _response_message(self, messages, **kwargs):
        del messages
        self._calls += 1
        self._tool_choices.append(kwargs.get("tool_choice"))
        if kwargs.get("tool_choice") == "none":
            return AIMessage(content="final-synthesis-sentinel")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "budget_probe",
                    "args": {"value": str(self._calls)},
                    "id": f"budget-probe-{self._calls}",
                    "type": "tool_call",
                }
            ],
        )


class _TaskOnceModel(MockAssistantChatModel):
    _calls: int = PrivateAttr(default=0)

    def _response_message(self, messages, **kwargs):
        del messages
        visible = _tool_names(kwargs.get("tools"))
        assert {"task", "write_todos"} <= visible
        self._calls += 1
        if self._calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "task-sentinel",
                            "subagent_type": "general-purpose",
                        },
                        "id": "task-sentinel",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="parent-complete-sentinel")


class _WriteOnceModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        del messages, kwargs
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_probe",
                    "args": {"value": "write-sentinel"},
                    "id": "write-probe-sentinel",
                    "type": "tool_call",
                }
            ],
        )


class _FilesystemWriteModel(MockAssistantChatModel):
    _calls: int = PrivateAttr(default=0)
    _visible_tools: list[set[str]] = PrivateAttr(default_factory=list)

    @property
    def visible_tools(self) -> tuple[set[str], ...]:
        return tuple(self._visible_tools)

    def _response_message(self, messages, **kwargs):
        del messages
        self._visible_tools.append(_tool_names(kwargs.get("tools")))
        self._calls += 1
        if self._calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/source.txt"},
                        "id": "hidden-read-file-sentinel",
                        "type": "tool_call",
                    }
                ],
            )
        if self._calls == 2:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "activate_tool_profile",
                        "args": {"profile_id": "filesystem"},
                        "id": "activate-filesystem-sentinel",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {
                        "file_path": "/sentinel.txt",
                        "content": "write-sentinel",
                    },
                    "id": "write-file-sentinel",
                    "type": "tool_call",
                }
            ],
        )


class _BrowserModel(MockAssistantChatModel):
    _calls: int = PrivateAttr(default=0)
    _visible_tools: list[set[str]] = PrivateAttr(default_factory=list)

    @property
    def visible_tools(self) -> tuple[set[str], ...]:
        return tuple(self._visible_tools)

    def _response_message(self, messages, **kwargs):
        del messages
        self._visible_tools.append(_tool_names(kwargs.get("tools")))
        self._calls += 1
        if self._calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "activate_tool_profile",
                        "args": {"profile_id": "browser"},
                        "id": "activate-browser-sentinel",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "mcp_playwright_browser_navigate",
                    "args": {"url": "https://example.test"},
                    "id": "browser-navigate-sentinel",
                    "type": "tool_call",
                }
            ],
        )


@pytest.mark.core_invariant("CTX-001")
def test_frozen_memory_is_transient_context_before_the_current_request(
    tmp_path: Path,
) -> None:
    model = _CaptureMessagesModel()
    model.observed_messages = []
    graph = _agent(tmp_path, model)
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": ("memory-sentinel",),
        },
        context=AssistantRunContext(),
    )

    model_humans = [
        item for item in model.observed_messages[-1] if isinstance(item, HumanMessage)
    ]
    state_humans = [
        item for item in result["messages"] if isinstance(item, HumanMessage)
    ]
    assert len(model_humans) == 2
    assert "memory-sentinel" in str(model_humans[-2].content)
    assert model_humans[-1].content == "request-sentinel"
    assert [item.content for item in state_humans] == ["request-sentinel"]


@pytest.mark.core_invariant("CTX-001")
def test_task_uses_narrow_read_only_worker_state_and_preserves_parent_handles(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    observed_worker_states: list[dict[str, Any]] = []

    def worker(state: dict[str, Any]) -> dict[str, Any]:
        observed_worker_states.append(state)
        return {
            "messages": [AIMessage(content="worker-complete-sentinel")],
            "active_tool_profile_ids": ["worker-profile-sentinel"],
            "provider_search_profile": "travel_general",
            "async_tasks": {"child-task-sentinel": {"status": "running"}},
        }

    parent_tasks = {"parent-task-sentinel": {"status": "running"}}
    graph = _agent(
        tmp_path,
        _TaskOnceModel(),
        worker=RunnableLambda(worker),
    )
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": ("memory-sentinel",),
            "memory_status": "ready",
            "provider_search_profile": "travel_general",
            "async_tasks": parent_tasks,
        },
        context=AssistantRunContext(),
    )

    worker_state = observed_worker_states[0]
    assert set(worker_state) == {"messages", "memory_context"}
    assert [message.content for message in worker_state["messages"]] == [
        "task-sentinel"
    ]
    assert "provider_search_profile" not in worker_state
    assert "async_tasks" not in worker_state
    assert result["async_tasks"] == parent_tasks
    assert "active_tool_profile_ids" not in result

    owner = asyncio.run(AgentServerExecutionOwner.compose(store=InMemoryStore()))
    try:
        worker_tools = set(
            owner.worker_graph.get_graph().nodes["tools"].data.tools_by_name
        )
        assert (
            not {
                "write_file",
                "edit_file",
                "delete",
                "execute",
                "task",
                "start_async_task",
            }
            & worker_tools
        )
        read_only_backend = ReadOnlyCodingWorkspaceBackend(
            SimpleNamespace(), "repo-sentinel"
        )
        with pytest.raises(NotImplementedError):
            read_only_backend.write("/blocked.txt", "blocked")
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("CTX-001")
def test_deep_agent_owns_summary_retry_todo_hitl_and_tool_policy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def probe(value: str) -> str:
        return value

    write_tool = StructuredTool.from_function(
        probe,
        name="write_probe",
        description="probe",
        metadata={"effect": "write"},
    )
    read_tool = StructuredTool.from_function(
        probe,
        name="read_probe",
        description="probe",
        metadata={"effect": "read"},
    )
    captured: dict[str, Any] = {}

    def recording_create_deep_agent(*args: Any, **kwargs: Any):
        del args
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        assistant_agent_module,
        "create_deep_agent",
        recording_create_deep_agent,
    )
    build_assistant_agent(
        MockAssistantChatModel(),
        [write_tool, read_tool],
        backend=object(),
        worker_graph=_worker(),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=project_tool_profiles(),
    )
    middleware = captured["middleware"]

    assert not any(isinstance(item, ModelCallLimitMiddleware) for item in middleware)
    assert any(isinstance(item, SummarizationMiddleware) for item in middleware)
    assert any(isinstance(item, TodoListMiddleware) for item in middleware)
    assert any(isinstance(item, ToolRetryMiddleware) for item in middleware)
    assert any(isinstance(item, ToolProfileMiddleware) for item in middleware)
    assert any(
        isinstance(item, ConditionalToolExposureMiddleware) for item in middleware
    )
    assert [
        item.max_parallel_calls_per_tool
        for item in middleware
        if isinstance(item, PerToolCallLimitMiddleware)
    ] == [12]
    assert [
        item.step_reserve
        for item in middleware
        if isinstance(item, RecursionFinalSynthesisMiddleware)
    ] == [8]
    assert set(captured["interrupt_on"]) >= {
        "write_file",
        "edit_file",
        "delete",
        "execute",
        "write_probe",
    }
    assert "read_probe" not in captured["interrupt_on"]
    assert [item["name"] for item in captured["subagents"]] == ["general-purpose"]
    filesystem_profile = next(
        profile
        for profile in project_tool_profiles()
        if profile.profile_id == "filesystem"
    )
    assert filesystem_profile.tool_names == (
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
    )


@pytest.mark.core_invariant("CTX-001")
def test_tool_policy_allows_twelve_parallel_calls_per_tool(tmp_path: Path) -> None:
    executed: list[str] = []

    class ParallelModel(MockAssistantChatModel):
        _calls: int = PrivateAttr(default=0)

        def _response_message(self, messages, **kwargs):
            del messages, kwargs
            self._calls += 1
            if self._calls > 1:
                return AIMessage(content="parallel-finished")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_probe",
                        "args": {"path": f"file-{index}.py"},
                        "id": f"call-{index}",
                        "type": "tool_call",
                    }
                    for index in range(13)
                ],
            )

    def read_probe(path: str) -> str:
        executed.append(path)
        return path

    tool = StructuredTool.from_function(
        read_probe,
        name="read_probe",
        description="probe",
        metadata={"effect": "read"},
    )
    result = _agent(tmp_path, ParallelModel(), [tool]).invoke(
        {"messages": [HumanMessage(content="parallel-request")]},
        context=AssistantRunContext(),
    )

    assert set(executed) == {f"file-{index}.py" for index in range(12)}
    assert isinstance(result["messages"][-1], AIMessage)


@pytest.mark.core_invariant("CTX-001")
def test_tool_policy_allows_identical_arguments_across_turns(tmp_path: Path) -> None:
    executed: list[str] = []

    class RepeatModel(MockAssistantChatModel):
        _calls: int = PrivateAttr(default=0)

        def _response_message(self, messages, **kwargs):
            del messages, kwargs
            self._calls += 1
            if self._calls > 2:
                return AIMessage(content="duplicate-finished")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_probe",
                        "args": {"path": "same.py"},
                        "id": f"duplicate-{self._calls}",
                        "type": "tool_call",
                    }
                ],
            )

    def read_probe(path: str) -> str:
        executed.append(path)
        return path

    tool = StructuredTool.from_function(
        read_probe,
        name="read_probe",
        description="probe",
        metadata={"effect": "read"},
    )
    _agent(tmp_path, RepeatModel(), [tool]).invoke(
        {"messages": [HumanMessage(content="duplicate-request")]},
        context=AssistantRunContext(),
    )

    assert executed == ["same.py", "same.py"]


@pytest.mark.core_invariant("CTX-001")
def test_remaining_graph_steps_force_tool_free_final_synthesis(tmp_path: Path) -> None:
    def budget_probe(value: str) -> str:
        return value

    tool = StructuredTool.from_function(
        budget_probe,
        name="budget_probe",
        description="probe",
        metadata={"effect": "read"},
    )
    model = _FinalSynthesisModel()
    result = _agent(tmp_path, model, [tool]).invoke(
        {"messages": [HumanMessage(content="budget-request")]},
        context=AssistantRunContext(),
        config={"recursion_limit": 12},
    )

    assert model.tool_choices[-1] == "none"
    assert isinstance(result["messages"][-1], AIMessage)
    assert not result["messages"][-1].tool_calls


@pytest.mark.core_invariant("CTX-001")
def test_unified_write_tool_interrupts_before_execution(tmp_path: Path) -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        executed.append(value)
        return value

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        description="probe",
        metadata={"effect": "write"},
    )
    result = _agent(tmp_path, _WriteOnceModel(), [tool]).invoke(
        {"messages": [HumanMessage(content="write-request-sentinel")]},
        context=AssistantRunContext(),
    )

    assert executed == []
    assert result["__interrupt__"][0].value["action_requests"][0]["name"] == (
        "write_probe"
    )


@pytest.mark.core_invariant("CTX-001")
def test_filesystem_profile_hides_tools_then_interrupts_write(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("source-sentinel", encoding="utf-8")
    model = _FilesystemWriteModel()
    result = _agent(
        tmp_path,
        model,
        tool_profiles=project_tool_profiles(),
    ).invoke(
        {"messages": [HumanMessage(content="write-file-request")]},
        context=AssistantRunContext(),
    )

    assert "read_file" not in model.visible_tools[0]
    assert "write_file" not in model.visible_tools[0]
    assert "read_file" not in model.visible_tools[1]
    assert "write_file" in model.visible_tools[2]
    blocked = next(
        item
        for item in result["messages"]
        if isinstance(item, ToolMessage)
        and item.tool_call_id == "hidden-read-file-sentinel"
    )
    assert blocked.status == "error"
    assert result["__interrupt__"][0].value["action_requests"][0]["name"] == (
        "write_file"
    )
    assert not (tmp_path / "sentinel.txt").exists()


@pytest.mark.core_invariant("CTX-001")
def test_browser_profile_hides_tool_then_interrupts_side_effect(tmp_path: Path) -> None:
    def navigate(url: str) -> str:
        return url

    browser_tool = StructuredTool.from_function(
        navigate,
        name="mcp_playwright_browser_navigate",
        description="probe",
        metadata={"effect": "dangerous", "source": "mcp"},
    )
    model = _BrowserModel()
    result = _agent(
        tmp_path,
        model,
        [browser_tool],
        tool_profiles=project_tool_profiles(),
    ).invoke(
        {"messages": [HumanMessage(content="browser-request")]},
        context=AssistantRunContext(),
    )

    assert "mcp_playwright_browser_navigate" not in model.visible_tools[0]
    assert "mcp_playwright_browser_navigate" in model.visible_tools[1]
    assert result["__interrupt__"][0].value["action_requests"][0]["name"] == (
        "mcp_playwright_browser_navigate"
    )

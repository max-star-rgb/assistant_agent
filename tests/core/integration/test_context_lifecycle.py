from __future__ import annotations

import asyncio
import json
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
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from langgraph_sdk.auth.types import StudioUser
from pydantic import PrivateAttr

from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.runtime.local_backend import ReadOnlyHomeBackend
from assistant_agent.native_agent import assistant_agent as assistant_agent_module
from assistant_agent.native_agent.assistant_agent import (
    RecursionFinalSynthesisMiddleware,
    build_assistant_agent,
    isolated_read_only_worker,
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


@pytest.mark.core_invariant("CTX-001")
def test_tool_profiles_only_keep_tools_registered_in_the_graph() -> None:
    middleware = ToolProfileMiddleware(
        project_tool_profiles(),
        available_tool_names={"read_file"},
    )

    assert [profile.profile_id for profile in middleware.profiles] == ["filesystem"]
    assert middleware.profiles[0].tool_names == ("read_file",)


@pytest.mark.core_invariant("CTX-001")
def test_empty_tool_profile_catalog_does_not_expose_activation_tool() -> None:
    middleware = ToolProfileMiddleware(
        project_tool_profiles(),
        available_tool_names=set(),
    )

    assert middleware.profiles == ()
    assert middleware.tools == []


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
    checkpointer=None,
    auto_approved_tool_names=frozenset(),
):
    return build_assistant_agent(
        model,
        tools,
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        worker_graph=worker or _worker(),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=tool_profiles,
        checkpointer=checkpointer,
        auto_approved_tool_names=auto_approved_tool_names,
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
        del kwargs
        if any(
            isinstance(message, ToolMessage)
            and message.tool_call_id == "write-probe-sentinel"
            for message in messages
        ):
            return AIMessage(content="write-complete-sentinel")
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
    monkeypatch,
    tmp_path: Path,
) -> None:
    class Memory:
        backend_id = "probe"

        async def recall(self, **_kwargs: Any):
            return ("memory-sentinel",)

        async def commit(self, **_kwargs: Any) -> None:
            return None

    class Threads:
        async def create(self, **kwargs: Any) -> dict[str, Any]:
            return {"thread_id": kwargs["thread_id"]}

    class Runs:
        async def list(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return []

        async def create(self, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(
        "assistant_agent.native_agent.memory_middleware.get_client",
        lambda: SimpleNamespace(threads=Threads(), runs=Runs()),
    )
    model = _CaptureMessagesModel()
    model.observed_messages = []
    graph = build_assistant_agent(
        model,
        [],
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        worker_graph=_worker(),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        memory_backend=Memory(),
        memory_extraction_delay_seconds=0,
    )
    result = graph.invoke(
        {"messages": [HumanMessage(content="request-sentinel")]},
        context=AssistantRunContext(),
        config={
            "configurable": {
                "thread_id": "memory-context-thread",
                "langgraph_auth_user": StudioUser("user-sentinel"),
            }
        },
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
            "messages": [
                AIMessage(content="worker-draft-sentinel", id="worker-draft"),
                AIMessage(content="worker-final-sentinel", id="worker-final"),
            ],
            "active_tool_profile_ids": ["worker-profile-sentinel"],
            "provider_search_profile": "deep_research",
            "async_tasks": {"child-task-sentinel": {"status": "running"}},
            "future_private_state": "private-sentinel",
        }

    parent_tasks = {"parent-task-sentinel": {"status": "running"}}
    parent_state = {
        "messages": [HumanMessage(content="task-sentinel")],
        "memory_context": ("memory-sentinel",),
        "provider_search_profile": "travel_general",
        "async_tasks": parent_tasks,
    }
    projected = isolated_read_only_worker(
        RunnableLambda(
            lambda state: {
                **worker(state),
                "structured_response": {"answer": "structured-sentinel"},
            }
        )
    ).invoke(parent_state)
    assert set(projected) == {"messages", "structured_response"}
    assert [message.id for message in projected["messages"]] == ["worker-final"]
    assert projected["structured_response"] == {"answer": "structured-sentinel"}
    assert parent_state["async_tasks"] == parent_tasks
    assert parent_state["provider_search_profile"] == "travel_general"
    observed_worker_states.clear()

    graph = _agent(
        tmp_path,
        _TaskOnceModel(),
        worker=RunnableLambda(worker),
    )
    result = graph.invoke(
        {"messages": [HumanMessage(content="request-sentinel")]},
        context=AssistantRunContext(),
    )

    worker_state = observed_worker_states[0]
    assert set(worker_state) == {"messages", "memory_context"}
    assert [message.content for message in worker_state["messages"]] == [
        "task-sentinel"
    ]
    assert "provider_search_profile" not in worker_state
    assert "async_tasks" not in worker_state
    assert "active_tool_profile_ids" not in result
    assert not {
        "worker-draft",
        "worker-final",
    } & {getattr(message, "id", None) for message in result["messages"]}
    task_result = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == "task-sentinel"
    )
    assert task_result.content == "worker-final-sentinel"

    owner = asyncio.run(AgentServerExecutionOwner.compose(store=InMemoryStore()))
    try:
        worker_tools = set(
            owner.worker_graph.get_graph().nodes["tools"].data.tools_by_name
        )
        assert {
            "calendar_search",
            "contacts_search",
            "email_read",
            "email_search",
            "live_view_inspect",
            "lodging_search",
            "uploaded_media_inspect",
            "visual_image_search",
            "visual_memory_search",
        } <= worker_tools
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
        read_only_backend = ReadOnlyHomeBackend(
            agent_home=Path.home() / "assistant_agent",
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

    sensitive_tool = StructuredTool.from_function(
        probe,
        name="sensitive_probe",
        description="probe",
    )
    delegated_tool = StructuredTool.from_function(
        probe,
        name="delegated_probe",
        description="probe",
    )
    browser_snapshot = StructuredTool.from_function(
        probe,
        name="mcp_playwright_browser_snapshot",
        description="probe",
    )
    browser_click = StructuredTool.from_function(
        probe,
        name="mcp_playwright_browser_click",
        description="probe",
    )
    captured: dict[str, Any] = {}
    browser_backend = object()

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
        [sensitive_tool, delegated_tool, browser_snapshot, browser_click],
        backend=object(),
        worker_graph=_worker(),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=project_tool_profiles(),
        general_purpose_tool_names={"delegated_probe"},
        auto_approved_tool_names={
            "delegated_probe",
            "mcp_playwright_browser_snapshot",
        },
        browser_tools=[browser_snapshot, browser_click],
        browser_backend=browser_backend,
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
        "sensitive_probe",
        "mcp_playwright_browser_click",
    }
    assert "delegated_probe" not in captured["interrupt_on"]
    assert "mcp_playwright_browser_snapshot" not in captured["interrupt_on"]
    subagents = {item["name"]: item for item in captured["subagents"]}
    assert set(subagents) == {"general-purpose", "coder", "browser-operator"}
    assert subagents["coder"]["tools"] == []
    assert {tool.name for tool in subagents["browser-operator"]["tools"]} == {
        "mcp_playwright_browser_snapshot",
        "mcp_playwright_browser_click",
    }
    assert set(subagents["browser-operator"]["interrupt_on"]) == {
        "mcp_playwright_browser_click"
    }
    browser_filesystem = subagents["browser-operator"]["middleware"][0]
    assert browser_filesystem.backend is browser_backend
    assert [tool.name for tool in browser_filesystem.tools] == [
        "ls",
        "read_file",
        "glob",
        "grep",
    ]
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
    )
    result = _agent(
        tmp_path,
        ParallelModel(),
        [tool],
        auto_approved_tool_names={"read_probe"},
    ).invoke(
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
    )
    _agent(
        tmp_path,
        RepeatModel(),
        [tool],
        auto_approved_tool_names={"read_probe"},
    ).invoke(
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
    )
    model = _FinalSynthesisModel()
    result = _agent(
        tmp_path,
        model,
        [tool],
        auto_approved_tool_names={"budget_probe"},
    ).invoke(
        {"messages": [HumanMessage(content="budget-request")]},
        context=AssistantRunContext(),
        config={"recursion_limit": 12},
    )

    assert model.tool_choices[-1] == "none"
    assert isinstance(result["messages"][-1], AIMessage)
    assert not result["messages"][-1].tool_calls


@pytest.mark.core_invariant("CTX-001")
@pytest.mark.parametrize(
    "decision,expected_executions", [("approve", 1), ("reject", 0)]
)
def test_unified_write_interrupt_resume_executes_at_most_once(
    tmp_path: Path,
    decision: str,
    expected_executions: int,
) -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        executed.append(value)
        return value

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        description="probe",
    )
    graph = _agent(
        tmp_path,
        _WriteOnceModel(),
        [tool],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": f"write-{decision}-sentinel"}}
    interrupted = graph.invoke(
        {"messages": [HumanMessage(content="write-request-sentinel")]},
        context=AssistantRunContext(),
        config=config,
    )

    assert executed == []
    assert (
        interrupted["__interrupt__"][0].value["action_requests"][0]["name"]
        == "write_probe"
    )

    decision_payload = {"type": decision}
    if decision == "reject":
        decision_payload["message"] = "rejected-sentinel"
    resumed = graph.invoke(
        Command(resume={"decisions": [decision_payload]}),
        context=AssistantRunContext(),
        config=config,
    )
    assert len(executed) == expected_executions
    assert (
        sum(
            isinstance(message, ToolMessage)
            and message.tool_call_id == "write-probe-sentinel"
            for message in resumed["messages"]
        )
        == 1
    )

    replayed = graph.invoke(
        Command(resume={"decisions": [decision_payload]}),
        context=AssistantRunContext(),
        config=config,
    )
    assert len(executed) == expected_executions
    assert (
        sum(
            isinstance(message, ToolMessage)
            and message.tool_call_id == "write-probe-sentinel"
            for message in replayed["messages"]
        )
        == 1
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
    assert "execute" in model.visible_tools[2]
    activated = next(
        item
        for item in result["messages"]
        if isinstance(item, ToolMessage)
        and item.tool_call_id == "activate-filesystem-sentinel"
    )
    assert json.loads(str(activated.content))["activated_tool_names"] == [
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
    ]
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
        metadata={"source": "mcp"},
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

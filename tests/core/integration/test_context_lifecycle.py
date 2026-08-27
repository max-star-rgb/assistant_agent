from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.middleware import FilesystemMiddleware
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import PrivateAttr

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import (
    RecursionFinalSynthesisMiddleware,
    build_fast_agent,
)
from assistant_agent.native_agent.planning_agent import build_planning_agent
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import (
    AssistantRootState,
    FastAgentState,
    PlanningAgentState,
    merge_async_tasks,
)
from assistant_agent.native_agent.tool_call_limits import PerToolCallLimitMiddleware
from assistant_agent.native_agent.tool_profiles import (
    ToolProfileMiddleware,
    project_tool_profiles,
)
from assistant_agent.skills.native import create_project_filesystem_backend


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


class _CaptureMessagesModel(MockAssistantChatModel):
    observed_messages: list[tuple[Any, ...]] = []

    def _response_message(self, messages, **kwargs):
        self.observed_messages.append(tuple(messages))
        return super()._response_message(messages, **kwargs)


class _CapturePlanningMessagesModel(MockAssistantChatModel):
    observed_calls: list[tuple[set[str], tuple[Any, ...]]] = []

    def _response_message(self, messages, **kwargs):
        self.observed_calls.append(
            (_tool_names(kwargs.get("tools")), tuple(messages))
        )
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


class _PlanningWriteModel(MockAssistantChatModel):
    _planning_calls: int = PrivateAttr(default=0)
    _subagent_runs: int = PrivateAttr(default=0)

    @property
    def subagent_runs(self) -> int:
        return self._subagent_runs

    def _response_message(self, messages, **kwargs):
        visible = _tool_names(kwargs.get("tools"))
        if {"task", "write_todos"} <= visible:
            self._planning_calls += 1
            if self._planning_calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "write one sentinel",
                                "subagent_type": "general-purpose",
                            },
                            "id": "task-write-sentinel",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="final-answer-sentinel")
        self._subagent_runs += 1
        if not any(
            isinstance(item, ToolMessage) and item.name == "write_probe"
            for item in messages
        ):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_probe",
                        "args": {"value": "worker-write-sentinel"},
                        "id": "worker-write-call",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="worker-write-complete")


class _FastWriteModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        del kwargs
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="completed:fast-write-sentinel")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_probe",
                    "args": {"value": "fast-write-sentinel"},
                    "id": "call-fast-write-sentinel",
                    "type": "tool_call",
                }
            ],
        )


class _FastFilesystemWriteModel(MockAssistantChatModel):
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
        if self._calls == 3:
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
        return AIMessage(content="unexpected-auto-approval")


class _FastBrowserModel(MockAssistantChatModel):
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
        if self._calls == 2:
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
        return AIMessage(content="unexpected-auto-approval")


class _PlanningNativeTaskModel(MockAssistantChatModel):
    _planning_calls: int = PrivateAttr(default=0)

    def _response_message(self, messages, **kwargs):
        del messages
        visible = _tool_names(kwargs.get("tools"))
        if {"task", "write_todos", "read_file"} <= visible:
            self._planning_calls += 1
            if self._planning_calls == 1:
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
            return AIMessage(content="planning-complete")
        raise AssertionError("worker uses a dedicated runnable")


@pytest.mark.core_invariant("CTX-001")
def test_frozen_memory_is_transient_context_before_the_current_request() -> None:
    model = _CaptureMessagesModel()
    model.observed_messages = []
    graph = build_fast_agent(model, [])
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": ("memory-sentinel",),
            "memory_status": "ready",
            "execution_mode": "fast",
        },
        context=AssistantRunContext(),
    )

    model_humans = [
        item for item in model.observed_messages[-1] if isinstance(item, HumanMessage)
    ]
    state_humans = [item for item in result["messages"] if isinstance(item, HumanMessage)]
    assert len(model_humans) == 2
    assert "memory-sentinel" in str(model_humans[-2].content)
    assert model_humans[-1].content == "request-sentinel"
    assert [item.content for item in state_humans] == ["request-sentinel"]


@pytest.mark.core_invariant("CTX-001")
def test_planning_and_task_receive_one_transient_frozen_memory_context() -> None:
    model = _CapturePlanningMessagesModel()
    model.observed_calls = []
    fast = build_fast_agent(model, [])
    graph = build_planning_agent(model, fast)
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": ("memory-sentinel",),
            "memory_status": "ready",
            "execution_mode": "planning",
        },
        context=AssistantRunContext(),
    )

    parent_calls = [
        messages
        for tools, messages in model.observed_calls
        if {"task", "write_todos"} <= tools
    ]
    child_call = next(
        messages
        for tools, messages in model.observed_calls
        if not {"task", "write_todos"} <= tools
    )
    for messages in (parent_calls[0], child_call):
        humans = [item for item in messages if isinstance(item, HumanMessage)]
        assert len(humans) == 2
        assert "memory-sentinel" in str(humans[-2].content)
        assert humans[-1].content == "request-sentinel"
    assert [
        item.content for item in result["messages"] if isinstance(item, HumanMessage)
    ] == ["request-sentinel"]


@pytest.mark.core_invariant("CTX-001")
def test_planning_task_receives_only_narrow_native_worker_state() -> None:
    observed_worker_states: list[dict[str, Any]] = []

    def worker(state: dict[str, Any]) -> dict[str, Any]:
        observed_worker_states.append(state)
        return {
            "messages": [AIMessage(content="worker-complete")],
            "active_tool_profile_ids": ["worker-profile-sentinel"],
            "skills_metadata": [{"name": "worker-skill-sentinel"}],
            "async_tasks": {"child-task-sentinel": {"status": "running"}},
        }

    graph = build_planning_agent(
        _PlanningNativeTaskModel(),
        RunnableLambda(worker),
    )
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": (),
            "memory_status": "empty",
            "execution_mode": "planning",
            "async_tasks": {"task-sentinel": {"status": "running"}},
        },
        context=AssistantRunContext(),
    )

    worker_state = observed_worker_states[0]
    assert [message.content for message in worker_state["messages"]] == [
        "task-sentinel"
    ]
    assert "todos" not in worker_state
    assert "skills_metadata" not in worker_state
    assert "loaded_skill_ids" not in worker_state
    assert "skill_reference_grants" not in worker_state
    assert "active_tool_profile_ids" not in result
    assert "skills_metadata" not in result
    assert "async_tasks" not in worker_state
    assert result["async_tasks"] == {
        "task-sentinel": {"status": "running"},
        "child-task-sentinel": {"status": "running"},
    }


@pytest.mark.core_invariant("CTX-001")
def test_async_task_handles_are_shared_across_fast_and_planning_modes() -> None:
    for state_schema in (AssistantRootState, FastAgentState, PlanningAgentState):
        assert "async_tasks" in state_schema.__annotations__
    assert merge_async_tasks(
        {"task-1": {"status": "running"}},
        {"task-1": {"status": "success"}, "task-2": {"status": "running"}},
    ) == {
        "task-1": {"status": "success"},
        "task-2": {"status": "running"},
    }


@pytest.mark.core_invariant("CTX-001")
def test_create_agent_owns_native_summary_retry_hitl_and_tool_call_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_agent.native_agent import fast_agent as fast_agent_module

    def probe(value: str) -> str:
        """Return one generic sentinel."""
        return value

    write_tool = StructuredTool.from_function(
        probe, name="write_probe", metadata={"effect": "write"}
    )
    read_tool = StructuredTool.from_function(
        probe, name="read_probe", metadata={"effect": "read"}
    )
    captured: list[object] = []
    real_create_agent = fast_agent_module.create_agent

    def recording_create_agent(*args: Any, **kwargs: Any):
        captured.extend(kwargs["middleware"])
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(fast_agent_module, "create_agent", recording_create_agent)
    graph = build_fast_agent(
        MockAssistantChatModel(),
        [write_tool, read_tool],
    )
    nodes = set(graph.get_graph().nodes)
    per_tool = [item for item in captured if isinstance(item, PerToolCallLimitMiddleware)]
    model_limits = [item for item in captured if isinstance(item, ModelCallLimitMiddleware)]
    finalizers = [
        item
        for item in captured
        if isinstance(item, RecursionFinalSynthesisMiddleware)
    ]

    assert model_limits == []
    assert [item.step_reserve for item in finalizers] == [8]
    assert [item.max_parallel_calls_per_tool for item in per_tool] == [12]
    assert any("SummarizationMiddleware" in node for node in nodes)
    assert any(isinstance(item, SummarizationMiddleware) for item in captured)
    assert any("HumanInTheLoopMiddleware" in node for node in nodes)
    assert any(isinstance(item, HumanInTheLoopMiddleware) for item in captured)
    assert any(isinstance(item, ToolRetryMiddleware) for item in captured)
    assert "SkillsMiddleware" in {type(item).__name__ for item in captured}
    skill_filesystems = [
        item for item in captured if isinstance(item, FilesystemMiddleware)
    ]
    assert len(skill_filesystems) == 1
    assert [tool.name for tool in skill_filesystems[0].tools] == [
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
    ]
    profile_middleware = [
        item for item in captured if isinstance(item, ToolProfileMiddleware)
    ]
    assert len(profile_middleware) == 1
    assert [tool.name for tool in profile_middleware[0].tools] == [
        "activate_tool_profile"
    ]
    travel_profile = next(
        profile
        for profile in project_tool_profiles()
        if profile.profile_id == "travel"
    )
    assert "lodging_search" in travel_profile.tool_names
    assert "mcp_amap_maps_maps_weather" not in travel_profile.tool_names
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
    )


@pytest.mark.core_invariant("CTX-001")
def test_tool_call_policy_allows_twelve_parallel_calls_per_tool() -> None:
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
        """Record one parallel read."""
        executed.append(path)
        return path

    agent = build_fast_agent(
        ParallelModel(),
        [
            StructuredTool.from_function(
                read_probe,
                name="read_probe",
                metadata={"effect": "read"},
            )
        ],
        filesystem_backend=FilesystemBackend(root_dir=Path.cwd(), virtual_mode=True),
        filesystem_tool_names=("read_file",),
        tool_profiles=(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="parallel-request")]},
        context=AssistantRunContext(),
    )

    assert set(executed) == {f"file-{index}.py" for index in range(12)}
    assert result["messages"][-1].content == "parallel-finished"


@pytest.mark.core_invariant("CTX-001")
def test_tool_call_policy_allows_identical_arguments_across_model_turns() -> None:
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
        """Record one repeated read."""
        executed.append(path)
        return path

    agent = build_fast_agent(
        RepeatModel(),
        [
            StructuredTool.from_function(
                read_probe,
                name="read_probe",
                metadata={"effect": "read"},
            )
        ],
        filesystem_backend=FilesystemBackend(root_dir=Path.cwd(), virtual_mode=True),
        filesystem_tool_names=("read_file",),
        tool_profiles=(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="duplicate-request")]},
        context=AssistantRunContext(),
    )

    assert executed == ["same.py", "same.py"]
    assert result["messages"][-1].content == "duplicate-finished"


@pytest.mark.core_invariant("CTX-001")
def test_fast_agent_uses_remaining_graph_steps_for_natural_synthesis() -> None:
    def budget_probe(value: str) -> str:
        """Return one recursion sentinel."""
        return value

    model = _FinalSynthesisModel()
    agent = build_fast_agent(
        model,
        [
            StructuredTool.from_function(
                budget_probe,
                name="budget_probe",
                metadata={"effect": "read"},
            )
        ],
        filesystem_backend=FilesystemBackend(root_dir=Path.cwd(), virtual_mode=True),
        filesystem_tool_names=("read_file",),
        tool_profiles=(),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="budget-request")]},
        context=AssistantRunContext(),
        config={"recursion_limit": 12},
    )

    assert model.tool_choices[-1] == "none"
    assert result["messages"][-1].content == "final-synthesis-sentinel"


@pytest.mark.core_invariant("CTX-001")
def test_planning_task_write_interrupts_and_resume_does_not_replay() -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record one approved write operation."""
        executed.append(value)
        return "write-complete"

    write_tool = StructuredTool.from_function(
        write_probe, name="write_probe", metadata={"effect": "write"}
    )
    model = _PlanningWriteModel()
    fast = build_fast_agent(model, [write_tool])
    planning = build_planning_agent(model, fast)
    builder = StateGraph(PlanningAgentState, context_schema=AssistantRunContext)
    builder.add_node("planning", planning)
    builder.add_edge(START, "planning")
    builder.add_edge("planning", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "native-task-hitl-thread"}}

    async def run_and_resume():
        interrupted = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "memory_context": (),
                "memory_status": "empty",
                "execution_mode": "planning",
            },
            config=config,
            context=AssistantRunContext(),
        )
        runs_before_resume = model.subagent_runs
        resumed = await graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=AssistantRunContext(),
        )
        return interrupted, resumed, runs_before_resume

    interrupted, resumed, runs_before_resume = asyncio.run(run_and_resume())
    assert interrupted["__interrupt__"][0].value["action_requests"][0]["name"] == "write_probe"
    assert executed == ["worker-write-sentinel"]
    assert runs_before_resume == 1
    assert model.subagent_runs == 2
    assert any(
        isinstance(item, ToolMessage)
        and item.tool_call_id == "task-write-sentinel"
        and item.content == "worker-write-complete"
        for item in resumed["messages"]
    )
    assert not any(
        isinstance(item, ToolMessage) and item.name == "write_probe"
        for item in resumed["messages"]
    )


@pytest.mark.core_invariant("CTX-001")
def test_fast_mode_write_tool_does_not_interrupt() -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record one fast-mode write operation."""
        executed.append(value)
        return "write-complete"

    tool = StructuredTool.from_function(
        write_probe, name="write_probe", metadata={"effect": "write"}
    )
    graph = build_fast_agent(_FastWriteModel(), [tool])
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="fast-write-sentinel")],
            "memory_context": (),
            "memory_status": "empty",
            "execution_mode": "fast",
        },
        context=AssistantRunContext(),
    )
    assert "__interrupt__" not in result
    assert executed == ["fast-write-sentinel"]


@pytest.mark.core_invariant("CTX-001")
def test_filesystem_defaults_to_project_and_accepts_explicit_home_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "project"
    home_root = tmp_path / "home"
    (project_root / "skills" / "demo").mkdir(parents=True)
    (project_root / "skills" / "demo" / "SKILL.md").write_text(
        "project skill",
        encoding="utf-8",
    )
    (project_root / "documents").mkdir(parents=True)
    (project_root / "documents" / "note.txt").write_text(
        "project note",
        encoding="utf-8",
    )
    (home_root / "documents").mkdir(parents=True)
    (home_root / "documents" / "note.txt").write_text("home note", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_root))

    backend = create_project_filesystem_backend(project_root)

    assert backend.read("/documents/note.txt").file_data["content"] == "project note"
    assert (
        backend.read(str(home_root / "documents" / "note.txt")).file_data["content"]
        == "home note"
    )
    assert backend.read("/skills/demo/SKILL.md").file_data["content"] == "project skill"


@pytest.mark.core_invariant("CTX-001")
def test_fast_filesystem_profile_requires_approval_before_writing(tmp_path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "source.txt").write_text("source-sentinel", encoding="utf-8")
    model = _FastFilesystemWriteModel()
    graph = build_fast_agent(
        model,
        [],
        filesystem_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )

    result = graph.invoke(
        {
            "messages": [HumanMessage(content="write-file-request")],
            "memory_context": (),
            "memory_status": "empty",
            "execution_mode": "fast",
        },
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
    assert result["__interrupt__"][0].value["action_requests"][0]["name"] == "write_file"
    assert not (tmp_path / "sentinel.txt").exists()


@pytest.mark.core_invariant("CTX-001")
def test_fast_browser_profile_hides_mcp_tools_until_activation() -> None:
    def navigate(url: str) -> str:
        """Navigate to a URL."""
        return url

    browser_tool = StructuredTool.from_function(
        navigate,
        name="mcp_playwright_browser_navigate",
        metadata={"effect": "dangerous", "source": "mcp"},
    )
    model = _FastBrowserModel()
    graph = build_fast_agent(model, [browser_tool])

    result = graph.invoke(
        {
            "messages": [HumanMessage(content="browser-request")],
            "memory_context": (),
            "memory_status": "empty",
            "execution_mode": "fast",
        },
        context=AssistantRunContext(),
    )

    assert "mcp_playwright_browser_navigate" not in model.visible_tools[0]
    assert "mcp_playwright_browser_navigate" in model.visible_tools[1]
    assert result["__interrupt__"][0].value["action_requests"][0]["name"] == (
        "mcp_playwright_browser_navigate"
    )

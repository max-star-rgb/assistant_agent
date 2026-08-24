from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolRuntime
from pydantic import PrivateAttr
import pytest

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent import memory_graph as memory_graph_module
from assistant_agent.native_agent import root_graph as root_graph_module
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.root_graph import build_assistant_root_graph
from assistant_agent.native_agent.state import (
    CodingState,
    FastAgentState,
    PlanningState,
)
from assistant_agent.skills.loading import SkillCatalog
from assistant_agent.tools.native_boundary import configure_builtin_tool
from scripts import run_server


class _User(dict):
    identity = "user-sentinel"
    permissions = ()


class _Memory:
    backend_id = "probe"

    def __init__(self) -> None:
        self.events = []

    async def recall(self, **_kwargs: Any):
        self.events.append("recall")
        return ("memory-sentinel",)

    async def commit(self, **_kwargs: Any):
        self.events.append("commit")


class _Runs:
    def __init__(self) -> None:
        self.cancellations: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    async def list(self, thread_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "run_id": f"memory-{thread_id}",
                "thread_id": thread_id,
                "assistant_id": "memory-assistant-sentinel",
                "status": "pending",
                "metadata": {"assistant_agent_run_kind": "memory_extraction"},
                "multitask_strategy": "enqueue",
            },
            {
                "run_id": f"chat-{thread_id}",
                "thread_id": thread_id,
                "assistant_id": "chat-assistant-sentinel",
                "status": "pending",
                "metadata": {},
                "multitask_strategy": "enqueue",
            },
        ]

    async def cancel(self, thread_id: str, run_id: str, **kwargs: Any) -> None:
        self.cancellations.append({"thread_id": thread_id, "run_id": run_id, **kwargs})

    async def create(self, **kwargs: Any) -> None:
        self.requests.append(kwargs)


class _Client:
    def __init__(self) -> None:
        self.runs = _Runs()


def _branch(schema, name):
    def answer(_state):
        return {"messages": [AIMessage(content="answer-sentinel")]}

    builder = StateGraph(schema, context_schema=AssistantRunContext)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    return builder.compile(name=name)


class _MemoryStatusPlanningModel(MockAssistantChatModel):
    _supervisor_calls: int = PrivateAttr(default=0)

    def _response_message(self, messages, **kwargs):
        tool_names = _model_tool_names(kwargs.get("tools"))
        if "WorkerResult" not in tool_names:
            self._supervisor_calls += 1
            if self._supervisor_calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_todos",
                            "args": {
                                "todos": [
                                    {
                                        "todo_id": "memory",
                                        "content": "echo-memory-status",
                                        "status": "pending",
                                    }
                                ]
                            },
                            "id": "memory-write-todos",
                            "type": "tool_call",
                        }
                    ],
                )
            if self._supervisor_calls == 2:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"todo_id": "memory"},
                            "id": "memory-task",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="memory-final-sentinel")
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and message.name == "memory_status_probe":
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "WorkerResult",
                            "args": {
                                "todo_id": "memory",
                                "status": "succeeded",
                                "summary": str(message.content),
                            },
                            "id": "memory-status-worker-result",
                            "type": "tool_call",
                        }
                    ],
                )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "memory_status_probe",
                    "args": {},
                    "id": "memory-status-tool-call",
                    "type": "tool_call",
                }
            ],
        )


def _planning_control_tools():
    @tool("load_skill")
    def load_skill(skill_id: str) -> str:
        """Load one generic probe Skill."""
        return skill_id

    @tool("load_skill_reference")
    def load_skill_reference(skill_id: str, reference_id: str) -> str:
        """Load one generic probe Skill reference."""
        return f"{skill_id}:{reference_id}"

    return [
        configure_builtin_tool(load_skill, "read"),
        configure_builtin_tool(load_skill_reference, "read"),
    ]


def _memory_status_tool():
    @tool("memory_status_probe")
    def memory_status_probe(
        runtime: ToolRuntime[AssistantRunContext],
    ) -> str:
        """Return the current worker memory status."""

        return str(runtime.state.get("memory_status", "missing"))

    return configure_builtin_tool(memory_status_probe, "read")


def _model_tool_names(raw_tools: object) -> set[str]:
    if not isinstance(raw_tools, list):
        return set()
    return {
        function["name"]
        for item in raw_tools
        if isinstance(item, dict)
        and isinstance((function := item.get("function")), dict)
        and isinstance(function.get("name"), str)
    }


@pytest.mark.core_invariant("MEMORY-001")
def test_chat_runs_recall_once_and_schedule_extraction_for_each_mode(
    monkeypatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(root_graph_module, "get_client", lambda: client, raising=False)

    async def run(mode):
        backend = _Memory()
        graph = build_assistant_root_graph(
            memory_backend=backend,
            fast_agent=_branch(FastAgentState, "AssistantFastAgent"),
            planning_graph=_branch(PlanningState, "AssistantPlanningGraph"),
            coding_graph=_branch(CodingState, "AssistantCodingGraph"),
        )
        result = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "execution_mode": mode,
            },
            context=AssistantRunContext(),
            config={
                "configurable": {
                    "thread_id": f"thread-{mode}-sentinel",
                    "assistant_id": "assistant-sentinel",
                    "graph_id": "graph-sentinel",
                    "langgraph_auth_user": _User(),
                }
            },
        )
        return backend.events, result

    async def run_all():
        return await asyncio.gather(run("fast"), run("planning"))

    results = asyncio.run(run_all())

    assert all(events == ["recall"] for events, _result in results)
    assert all(
        result["memory_context"] == ("memory-sentinel",) for _events, result in results
    )
    assert sorted(client.runs.cancellations, key=lambda item: item["thread_id"]) == [
        {
            "thread_id": "thread-fast-sentinel",
            "run_id": "memory-thread-fast-sentinel",
            "wait": True,
            "action": "rollback",
        },
        {
            "thread_id": "thread-planning-sentinel",
            "run_id": "memory-thread-planning-sentinel",
            "wait": True,
            "action": "rollback",
        },
    ]
    assert len(client.runs.requests) == 2
    assert all(
        request["assistant_id"] == "assistant-memory-v1"
        and request["metadata"] == {"assistant_agent_run_kind": "memory_extraction"}
        and request["after_seconds"] == 1800
        and request["multitask_strategy"] == "enqueue"
        for request in client.runs.requests
    )


@pytest.mark.core_invariant("MEMORY-001")
def test_planning_worker_preserves_parent_memory_status() -> None:
    model = _MemoryStatusPlanningModel()
    memory_status_tool = _memory_status_tool()
    tools = [*_planning_control_tools(), memory_status_tool]
    fast_agent = build_fast_agent(
        model,
        tools,
        skill_catalog=SkillCatalog(),
    )
    graph = build_planning_graph(
        model,
        fast_agent,
        tools=tools,
        skill_catalog=SkillCatalog(),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "memory_context": (),
                "memory_status": "degraded",
            },
            context=AssistantRunContext(),
        )
    )

    assert result["worker_results"]["memory"]["summary"] == "degraded"


@pytest.mark.core_invariant("MEMORY-001")
def test_independent_memory_graph_extracts_without_recall_or_agent() -> None:
    backend = _Memory()
    build_memory_extraction_graph = getattr(
        memory_graph_module,
        "build_memory_extraction_graph",
        None,
    )
    assert callable(build_memory_extraction_graph)
    graph = build_memory_extraction_graph(backend=backend)

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [
                    HumanMessage(content="request-sentinel"),
                    AIMessage(content="existing-answer-sentinel"),
                ],
            },
            context=AssistantRunContext(),
            config={
                "configurable": {
                    "assistant_id": "assistant-sentinel",
                    "graph_id": "graph-sentinel",
                    "langgraph_auth_user": _User(),
                }
            },
        )
    )

    assert backend.events == ["commit"]
    assert [message.content for message in result["messages"]] == [
        "request-sentinel",
        "existing-answer-sentinel",
    ]


@pytest.mark.core_invariant("MEMORY-001")
def test_dev_server_keeps_capacity_for_chat_while_memory_extracts(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        run_server,
        "hold_dev_server_lock",
        lambda: nullcontext(),
    )
    monkeypatch.setattr(run_server, "require_available_port", lambda *_args: None)

    def capture_command(command, **_kwargs):
        captured["command"] = list(command)
        config_index = captured["command"].index("--config")
        config_path = Path(captured["command"][config_index + 1])
        captured["config"] = json.loads(config_path.read_text(encoding="utf-8"))
        return 0

    monkeypatch.setattr(run_server, "run_command_with_log", capture_command)

    assert run_server.main(["--backend", "dev", "--no-env-file"]) == 0

    command = captured["command"]
    option_index = command.index("--n-jobs-per-worker")
    assert int(command[option_index + 1]) >= 2
    assert captured["config"]["env"] == {}

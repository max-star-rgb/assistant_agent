from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
import pytest

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent import memory_graph as memory_graph_module
from assistant_agent.native_agent import root_graph as root_graph_module
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.root_graph import build_assistant_root_graph
from assistant_agent.native_agent.state import FastAgentState, PlanningState


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
        self.cancellations.append(
            {"thread_id": thread_id, "run_id": run_id, **kwargs}
        )

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


def _memory_status_echo_agent():
    def answer(state):
        return {
            "messages": [
                AIMessage(content=str(state.get("memory_status", "missing")))
            ]
        }

    builder = StateGraph(FastAgentState, context_schema=AssistantRunContext)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    return builder.compile(name="AssistantFastAgent")


@pytest.mark.core_invariant("MEMORY-001")
def test_chat_runs_recall_once_and_schedule_extraction_for_each_mode(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(root_graph_module, "get_client", lambda: client, raising=False)

    async def run(mode):
        backend = _Memory()
        graph = build_assistant_root_graph(
            memory_backend=backend,
            fast_agent=_branch(FastAgentState, "AssistantFastAgent"),
            planning_graph=_branch(PlanningState, "AssistantPlanningGraph"),
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
    assert all(result["memory_context"] == ("memory-sentinel",) for _events, result in results)
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
        and request["metadata"]
        == {"assistant_agent_run_kind": "memory_extraction"}
        and request["after_seconds"] == 1800
        and request["multitask_strategy"] == "enqueue"
        for request in client.runs.requests
    )


@pytest.mark.core_invariant("MEMORY-001")
def test_planning_worker_preserves_parent_memory_status() -> None:
    model = MockAssistantChatModel()
    graph = build_planning_graph(model, _memory_status_echo_agent())

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

    assert [item.content for item in result["worker_results"]] == ["degraded"]


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

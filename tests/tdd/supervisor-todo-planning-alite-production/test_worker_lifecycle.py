from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import PlanningState
from assistant_agent.skills.loading import SkillCatalog
from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    create_load_skill_reference_tool,
    create_load_skill_tool,
)

from probes import (
    BarrierWorkerModel,
    OperationalFailureWorkerModel,
    SequencedWorkerModel,
    ScriptedSupervisor,
    ToolLoopWorkerModel,
    tool_calls,
)


def _tools():
    return [create_load_skill_tool(), create_load_skill_reference_tool()]


def _graph(supervisor, worker, *, business_tools=()):
    catalog = SkillCatalog()
    tools = [*_tools(), *business_tools]
    fast = build_fast_agent(
        worker,
        tools,
        skill_catalog=catalog,
        model_call_limit=8,
        tool_call_limit=8,
    )
    return build_planning_graph(
        supervisor,
        fast,
        tools=tools,
        skill_catalog=catalog,
    )


def _input(parent_text: str = "parent-secret-sentinel") -> dict[str, object]:
    return {
        "messages": [HumanMessage(content=parent_text)],
        "memory_context": (),
        "memory_status": "empty",
        "todos": [],
        "worker_results": {},
        "worker_writes": [],
        "active_skill_ids": [],
        "skill_reference_grants": {},
    }


def test_three_tasks_run_in_parallel_join_and_keep_worker_context_private() -> None:
    supervisor = ScriptedSupervisor.parallel_wave(("A", "B", "C"))
    worker = BarrierWorkerModel(expected_todos={"A", "B", "C"})

    result = asyncio.run(_graph(supervisor, worker).ainvoke(_input()))

    assert worker.max_concurrency == 3
    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1})
    assert set(result["worker_results"]) == {"A", "B", "C"}
    assert all(item["status"] == "completed" for item in result["todos"])
    assert {
        message.tool_call_id
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.name == "task"
    } == {"task-A", "task-B", "task-C"}
    assert all(
        "parent-secret-sentinel" not in str(payload) for payload in worker.payloads
    )


def test_parent_graph_exposes_only_alite_planning_roles() -> None:
    graph = _graph(
        ScriptedSupervisor.parallel_wave(("A",)),
        BarrierWorkerModel(expected_todos={"A"}),
    )
    nodes = set(graph.get_graph().nodes)
    assert {"supervisor", "controls", "worker", "join"} <= nodes
    assert nodes.isdisjoint(
        {"planner", "scheduler", "finalizer", "recovery", "reserve_wave_budget"}
    )


def test_pending_writes_resume_only_the_operationally_failed_worker() -> None:
    supervisor = ScriptedSupervisor.parallel_wave(("A", "B", "C"))
    worker = OperationalFailureWorkerModel(
        expected_todos={"A", "B", "C"}, fail_once_for="C"
    )
    planning = _graph(supervisor, worker)
    builder = StateGraph(PlanningState)
    builder.add_node("planning", planning)
    builder.add_edge(START, "planning")
    builder.add_edge("planning", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "alite-production-pending-writes"}}

    with pytest.raises(TimeoutError, match="C-operational-sentinel"):
        asyncio.run(graph.ainvoke(_input(), config=config))

    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1})
    result = asyncio.run(graph.ainvoke(None, config=config))
    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 2})
    assert set(result["worker_results"]) == {"A", "B", "C"}


def test_blocked_todo_stays_pending_and_supervisor_can_retry_only_that_todo() -> None:
    supervisor = ScriptedSupervisor(
        [
            tool_calls(
                (
                    "write_todos",
                    {
                        "todos": [
                            {"todo_id": "A", "content": "todo-A", "status": "pending"}
                        ]
                    },
                    "write-initial",
                )
            ),
            tool_calls(("task", {"todo_id": "A"}, "task-A-1")),
            tool_calls(("task", {"todo_id": "A"}, "task-A-2")),
            AIMessage(content="final-after-retry"),
        ]
    )
    worker = SequencedWorkerModel(outcomes={"A": ["blocked", "succeeded"]})

    result = asyncio.run(_graph(supervisor, worker).ainvoke(_input()))

    assert worker.calls_by_todo == Counter({"A": 2})
    assert result["worker_results"]["A"]["status"] == "succeeded"
    assert result["todos"] == [
        {"todo_id": "A", "content": "todo-A", "status": "completed"}
    ]


def test_rewriting_pending_content_clears_the_old_blocked_result() -> None:
    supervisor = ScriptedSupervisor(
        [
            tool_calls(
                (
                    "write_todos",
                    {"todos": [{"todo_id": "A", "content": "old", "status": "pending"}]},
                    "write-old",
                )
            ),
            tool_calls(("task", {"todo_id": "A"}, "task-old")),
            tool_calls(
                (
                    "write_todos",
                    {"todos": [{"todo_id": "A", "content": "new", "status": "pending"}]},
                    "write-new",
                )
            ),
            AIMessage(content="final-after-rewrite"),
        ]
    )
    worker = SequencedWorkerModel(outcomes={"A": ["blocked"]})

    result = asyncio.run(_graph(supervisor, worker).ainvoke(_input()))

    assert result["todos"] == [
        {"todo_id": "A", "content": "new", "status": "pending"}
    ]
    assert result["worker_results"] == {}


def test_worker_keeps_its_business_tool_loop_inside_the_subgraph() -> None:
    @tool("business_probe")
    def business_probe(todo_id: str) -> dict[str, str]:
        """Return deterministic read-only business evidence."""

        return {"todo_id": todo_id, "evidence": "private-worker-sentinel"}

    business_probe.metadata = {"effect": "read", "source": "test"}
    worker = ToolLoopWorkerModel()
    result = asyncio.run(
        _graph(
            ScriptedSupervisor.parallel_wave(("A",)),
            worker,
            business_tools=(business_probe,),
        ).ainvoke(_input())
    )

    assert any(
        isinstance(message, ToolMessage) and message.name == "business_probe"
        for turn in worker.seen_messages
        for message in turn
    )
    assert not any(
        isinstance(message, ToolMessage) and message.name == "business_probe"
        for message in result["messages"]
    )


def test_repository_mock_model_exercises_the_complete_alite_loop() -> None:
    model = MockAssistantChatModel()

    result = asyncio.run(_graph(model, model).ainvoke(_input("mock planning goal")))

    assert result["todos"] == [
        {"todo_id": "answer", "content": "mock planning goal", "status": "completed"}
    ]
    assert result["worker_results"]["answer"]["status"] == "succeeded"
    assert result["messages"][-1].content == "已完成 planning mock：mock worker completion"

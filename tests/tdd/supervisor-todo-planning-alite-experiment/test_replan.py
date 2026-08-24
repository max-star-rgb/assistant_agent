from __future__ import annotations

import asyncio
from collections import Counter

from langchain_core.messages import AIMessage, HumanMessage

from experiment_graph import build_experiment_graph
from probes import ScenarioWorkerModel, ScriptedSupervisor


def _initial_input() -> dict[str, object]:
    return {
        "messages": [HumanMessage(content="request-sentinel")],
        "todos": [],
        "worker_results": {},
        "worker_writes": [],
        "loaded_skills": [],
        "trusted_context": {},
        "join_count": 0,
    }


def test_blocked_c_can_retry_without_replaying_a_or_b() -> None:
    supervisor = ScriptedSupervisor.blocked_then_retry("C")
    worker = ScenarioWorkerModel(
        outcomes={
            "A": ["succeeded"],
            "B": ["succeeded"],
            "C": ["blocked", "succeeded"],
        }
    )
    graph = build_experiment_graph(supervisor, worker)

    result = asyncio.run(graph.ainvoke(_initial_input()))

    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 2})
    assert {
        item["todo_id"]
        for item in result["todos"]
        if item["status"] == "completed"
    } == {"A", "B", "C"}
    assert result["worker_results"]["A"]["summary"] == "A-success-sentinel"
    assert result["worker_results"]["B"]["summary"] == "B-success-sentinel"


def test_replan_replaces_pending_c_and_preserves_completed_results() -> None:
    supervisor = ScriptedSupervisor.blocked_c_then_replace_with_d()
    worker = ScenarioWorkerModel(
        outcomes={
            "A": ["succeeded"],
            "B": ["succeeded"],
            "C": ["blocked"],
            "D": ["succeeded"],
        }
    )

    result = asyncio.run(
        build_experiment_graph(supervisor, worker).ainvoke(_initial_input())
    )

    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1, "D": 1})
    assert [item["todo_id"] for item in result["todos"]] == ["A", "B", "D"]
    assert set(result["worker_results"]) == {"A", "B", "C", "D"}
    assert result["worker_results"]["A"]["status"] == "succeeded"
    assert result["worker_results"]["B"]["status"] == "succeeded"


def test_supervisor_can_finish_after_blocked_c() -> None:
    supervisor = ScriptedSupervisor.blocked_c_then_finish()
    worker = ScenarioWorkerModel(
        outcomes={"A": ["succeeded"], "B": ["succeeded"], "C": ["blocked"]}
    )

    result = asyncio.run(
        build_experiment_graph(supervisor, worker).ainvoke(_initial_input())
    )

    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1})
    assert result["todos"][-1] == {
        "todo_id": "C",
        "content": "todo-C",
        "status": "pending",
    }
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].tool_calls == []

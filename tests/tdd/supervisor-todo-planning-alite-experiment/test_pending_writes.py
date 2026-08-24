from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from experiment_graph import build_experiment_graph
from probes import OperationalFailureWorkerModel, ScriptedSupervisor


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


def test_pending_writes_resume_only_failed_worker() -> None:
    saver = InMemorySaver()
    supervisor = ScriptedSupervisor.parallel_wave(("A", "B", "C"))
    worker = OperationalFailureWorkerModel(
        expected_todos={"A", "B", "C"},
        fail_once_for="C",
    )
    graph = build_experiment_graph(
        supervisor,
        worker,
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": "alite-pending-writes"}}

    with pytest.raises(TimeoutError, match="C-operational-sentinel"):
        asyncio.run(graph.ainvoke(_initial_input(), config=config))

    failed_snapshot = graph.get_state(config)
    assert failed_snapshot.values.get("join_count", 0) == 0
    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1})
    assert sum(task.error is not None for task in failed_snapshot.tasks) == 1

    result = asyncio.run(graph.ainvoke(None, config=config))

    assert worker.calls_by_todo == Counter({"A": 1, "B": 1, "C": 2})
    assert result["join_count"] == 1
    assert set(result["worker_results"]) == {"A", "B", "C"}
    assert all(item["status"] == "completed" for item in result["todos"])

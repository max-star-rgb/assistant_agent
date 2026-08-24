from __future__ import annotations

import asyncio
import json
from collections import Counter

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

import experiment_graph
from experiment_graph import build_experiment_graph
from probes import (
    BarrierWorkerModel,
    ScriptedSupervisor,
    ToolCallingWorkerModel,
    create_read_probe_tool,
)


def _initial_input(parent_text: str = "request-sentinel") -> dict[str, object]:
    return {
        "messages": [HumanMessage(content=parent_text)],
        "todos": [],
        "worker_results": {},
        "worker_writes": [],
        "loaded_skills": ["skill-sentinel"],
        "trusted_context": {"timezone": "Asia/Shanghai"},
        "join_count": 0,
    }


def test_three_tasks_run_in_parallel_and_join_once() -> None:
    supervisor = ScriptedSupervisor.parallel_wave(("A", "B", "C"))
    worker_model = BarrierWorkerModel(expected_todos={"A", "B", "C"})
    graph = build_experiment_graph(supervisor, worker_model)

    result = asyncio.run(
        graph.ainvoke(_initial_input("parent-secret-sentinel"))
    )

    assert worker_model.max_concurrency == 3
    assert worker_model.calls_by_todo == Counter({"A": 1, "B": 1, "C": 1})
    assert result["join_count"] == 1
    assert set(result["worker_results"]) == {"A", "B", "C"}
    assert {
        message.tool_call_id
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.name == "task"
    } == {"task-A", "task-B", "task-C"}
    assert {payload["todo_id"] for payload in worker_model.private_payloads} == {
        "A",
        "B",
        "C",
    }
    assert all(
        payload["loaded_skills"] == ["skill-sentinel"]
        and payload["trusted_context"] == {"timezone": "Asia/Shanghai"}
        and "parent-secret-sentinel" not in json.dumps(payload)
        for payload in worker_model.private_payloads
    )


def test_worker_uses_private_create_agent_tool_loop() -> None:
    recorder: list[str] = []
    graph = build_experiment_graph(
        ScriptedSupervisor.single_success("A"),
        ToolCallingWorkerModel(todo_id="A"),
        read_probe_tool=create_read_probe_tool(recorder),
    )

    result = asyncio.run(
        graph.ainvoke(_initial_input("parent-secret-sentinel"))
    )

    assert recorder == ["A"]
    assert result["worker_results"]["A"]["summary"] == (
        "read-probe-result-sentinel"
    )
    assert not any(
        isinstance(message, ToolMessage) and message.name == "read_probe"
        for message in result["messages"]
    )


def test_graph_builds_exactly_one_worker_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    real_create_agent = experiment_graph.create_agent
    calls = 0

    def recording_create_agent(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(experiment_graph, "create_agent", recording_create_agent)

    build_experiment_graph(
        ScriptedSupervisor.single_success("A"),
        BarrierWorkerModel(expected_todos={"A"}),
    )

    assert calls == 1


def test_task_must_reference_an_existing_pending_todo() -> None:
    supervisor = ScriptedSupervisor.task_without_todo("missing")
    graph = build_experiment_graph(
        supervisor,
        BarrierWorkerModel(expected_todos={"missing"}),
    )

    with pytest.raises(ValueError, match="non-pending todo missing"):
        asyncio.run(graph.ainvoke(_initial_input()))

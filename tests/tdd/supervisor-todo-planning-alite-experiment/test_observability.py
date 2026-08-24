from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage

from experiment_graph import build_experiment_graph
from probes import (
    ScenarioWorkerModel,
    ScriptedSupervisor,
    ToolCallingWorkerModel,
    create_read_probe_tool,
)


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


def test_parent_graph_contains_only_alite_runtime_roles() -> None:
    graph = build_experiment_graph(
        ScriptedSupervisor.single_success("A"),
        ScenarioWorkerModel(outcomes={"A": ["succeeded"]}),
    )
    nodes = set(graph.get_graph().nodes)

    assert {"supervisor", "controls", "worker", "join"} <= nodes
    assert nodes.isdisjoint(
        {"planner", "scheduler", "finalizer", "recovery", "reserve_wave_budget"}
    )


def test_worker_create_agent_is_visible_in_native_subgraph_stream() -> None:
    recorder: list[str] = []
    graph = build_experiment_graph(
        ScriptedSupervisor.single_success("A"),
        ToolCallingWorkerModel(todo_id="A"),
        read_probe_tool=create_read_probe_tool(recorder),
    )

    async def collect() -> list[dict[str, object]]:
        return [
            part
            async for part in graph.astream(
                _initial_input(),
                stream_mode=["updates", "messages"],
                subgraphs=True,
                version="v2",
            )
        ]

    parts = asyncio.run(collect())
    worker_parts = [
        part
        for part in parts
        if part["ns"] and str(part["ns"][0]).startswith("worker:")
    ]

    assert recorder == ["A"]
    assert worker_parts
    assert any(
        part["type"] == "updates"
        and set(part["data"]).intersection({"model", "tools"})
        for part in worker_parts
    )

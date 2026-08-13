from __future__ import annotations

import asyncio
import json
import random

from langgraph.runtime import Runtime

from assistant_agent.workflows.durable_graph_nodes import prepare_next_wave_node
from assistant_agent.workflows.graph_state import latest_results
from assistant_agent.workflows.graph_state import PersistedAdmittedWorkflowPlan

from workflow_graph_probe import config, workflow_probe


def _updates(parts):
    return [part for part in parts if part.get("type") == "updates"]


def test_send_runs_ready_nodes_in_one_superstep_and_join_waits_for_all(tmp_path):
    app, context, initial, worker, artifact_store = workflow_probe(
        tmp_path, {"a": [], "b": [], "c": ["a", "b"]}
    )

    async def execute():
        parts = [
            part
            async for part in app.astream(
                initial,
                config=config(),
                context=context,
                stream_mode=["updates", "tasks", "checkpoints"],
                subgraphs=True,
                version="v2",
            )
        ]
        return parts, await app.aget_state(config())

    try:
        parts, snapshot = asyncio.run(execute())
        final = snapshot.values
        assert worker.max_concurrency == 2
        assert final["wave_history"] == [["a", "b"], ["c"]]
        assert set(
            latest_results(
                final["result_ledger"], final["execution_generation_by_node"]
            )
        ) == {"a", "b", "c"}
        update_nodes = [
            next(iter(part["data"])) for part in _updates(parts) if part.get("data")
        ]
        first_join = update_nodes.index("join_wave")
        worker_positions = [
            index for index, name in enumerate(update_nodes) if name == "run_worker"
        ]
        assert len(worker_positions) == 3
        assert update_nodes.count("join_wave") == 2
        assert first_join > worker_positions[0]
        assert first_join > worker_positions[1]
        namespaces = [tuple(part.get("ns") or ()) for part in parts]
        flattened = "/".join(
            segment for namespace in namespaces for segment in namespace
        )
        assert "run_worker:" in flattened
        assert "worker_profile:" in flattened
        checkpoint_json = json.dumps(final, ensure_ascii=False, default=str)
        assert '"workflow_control"' not in checkpoint_json
        assert "workflow-artifact://" not in checkpoint_json
        results = latest_results(
            final["result_ledger"], final["execution_generation_by_node"]
        )
        assert results["a"].artifact_refs == ()
    finally:
        artifact_store.close()


def test_arbitrary_dag_wave_partition_is_order_independent(tmp_path):
    items = [("a", []), ("b", []), ("c", ["a", "b"]), ("d", ["b"]), ("e", ["d"])]
    observed = set()
    randomizer = random.Random(7)
    for index in range(20):
        randomizer.shuffle(items)
        graph_dir = tmp_path / str(index)
        app, context, initial, _worker, artifact_store = workflow_probe(
            graph_dir, dict(items)
        )
        try:
            final = asyncio.run(app.ainvoke(initial, config=config(), context=context))
            observed.add(tuple(tuple(wave) for wave in final["wave_history"]))
        finally:
            artifact_store.close()
    assert observed == {(("a", "b"), ("c", "d"), ("e",))}


def test_stalled_dag_fails_closed_without_polling(tmp_path):
    app, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"a": [], "b": ["a"]}
    )
    try:
        completed = asyncio.run(app.ainvoke(initial, config=config(), context=context))
        plan = PersistedAdmittedWorkflowPlan.model_validate_json(
            json.dumps(completed["admitted_plan"])
        )
        # Bypass model validation to emulate a corrupted restored checkpoint.
        # The execution node must not poll or invent a ready item.
        cyclic_nodes = (
            plan.nodes[0].model_copy(update={"depends_on": ("b",)}),
            plan.nodes[1].model_copy(update={"depends_on": ("a",)}),
        )
        corrupted = dict(completed)
        corrupted.update(
            admitted_plan=plan.model_copy(update={"nodes": cyclic_nodes}),
            status="running",
            phase="executing",
            result_ledger={},
            active_wave=(),
        )
        update = prepare_next_wave_node(corrupted, Runtime(context=context))
        assert update["status"] == "failed"
        assert any(error.code == "workflow_dag_stalled" for error in update["errors"])
    finally:
        artifact_store.close()

from __future__ import annotations

import asyncio

from assistant_agent.workflows import planning as legacy_planning
from assistant_agent.workflows import store as legacy_store
from assistant_agent.workflows import worker as legacy_worker
from assistant_agent.workflows.durable_graph_app import (
    DurableWorkflowGraphApp,
    WorkflowGraphExecutionIdentity,
)
from assistant_agent.workflows.observed_store import ObservedWorkflowStore
from assistant_agent.workflows.runtime import WorkflowRuntime
from assistant_agent.workflows.store import InMemoryWorkflowStore

from workflow_graph_probe import workflow_probe


def _identity() -> WorkflowGraphExecutionIdentity:
    return WorkflowGraphExecutionIdentity(
        workflow_id="wf-send",
        thread_id="workflow-thread-send",
        run_id="invoke-send",
        user_id="user-send",
        session_id="session-send",
        agent_id="agent-send",
    )


def test_native_graph_execution_never_calls_legacy_scheduler_boundaries(
    tmp_path, monkeypatch
) -> None:
    """A graph_v3 execution must fail this test if a shadow scheduler is added."""

    def unexpected_legacy_call(*_args, **_kwargs):
        raise AssertionError("native graph called a legacy scheduler boundary")

    monkeypatch.setattr(WorkflowRuntime, "run_claim", unexpected_legacy_call)
    monkeypatch.setattr(
        legacy_worker.DurableWorkflowWorker, "run_once", unexpected_legacy_call
    )
    monkeypatch.setattr(
        InMemoryWorkflowStore, "claim_ready_work_item", unexpected_legacy_call
    )
    monkeypatch.setattr(
        InMemoryWorkflowStore, "renew_work_item_lease", unexpected_legacy_call
    )
    monkeypatch.setattr(
        ObservedWorkflowStore, "claim_ready_work_item", unexpected_legacy_call
    )
    monkeypatch.setattr(
        ObservedWorkflowStore, "renew_work_item_lease", unexpected_legacy_call
    )
    monkeypatch.setattr(
        legacy_planning, "next_ready_work_item", unexpected_legacy_call
    )
    monkeypatch.setattr(
        legacy_store, "claim_ready_item_in_bundle", unexpected_legacy_call
    )
    monkeypatch.setattr(legacy_worker, "ThreadPoolExecutor", unexpected_legacy_call)

    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"collect": [], "write": ["collect"]}
    )
    app = DurableWorkflowGraphApp(graph)
    try:
        result = asyncio.run(
            app.arun(initial, identity=_identity(), context=context)
        )
        assert result.status == "completed"
        assert result.final_state["execution_engine"] == "langgraph_v3"
        assert result.final_state["wave_history"] == [["collect"], ["write"]]
    finally:
        artifact_store.close()

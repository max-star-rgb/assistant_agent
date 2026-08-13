from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import replace

from assistant_agent.workflows import planning as legacy_planning
from assistant_agent.workflows import store as legacy_store
from assistant_agent.workflows import worker as legacy_worker
from assistant_agent.workflows.durable_graph_app import (
    DurableWorkflowGraphApp,
    WorkflowGraphExecutionIdentity,
)
from assistant_agent.workflows.runtime import WorkflowRuntime
from assistant_agent.workflows.store import InMemoryWorkflowStore
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import MockChatAdapter
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.workflows.graph_host import WorkflowGraphHandle

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
    """A graph_v3 execution must not restore the deleted shadow-store scheduler."""

    assert importlib.util.find_spec(
        "assistant_agent.workflows.observed_store"
    ) is None

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


def test_async_deep_research_uses_graph_host_and_never_legacy_submit() -> None:
    """Restoring the old service submit call must fail this production cutover test."""

    class ForbiddenLegacyService:
        def submit(self, **_kwargs):
            raise AssertionError("deep research called legacy workflow submit")

    class RecordingGraphHost:
        def __init__(self) -> None:
            self.calls = []

        async def start(self, **kwargs):
            self.calls.append(kwargs)
            return WorkflowGraphHandle(
                workflow_id="workflow-native-cutover",
                workflow_type="deep_research",
                execution_engine="langgraph_v3",
                status="queued",
                phase="planning",
                output_ref="workflow://workflow-native-cutover",
            )

    registry = ToolRegistry()
    registry.seal()
    host = RecordingGraphHost()
    runtime = AgentGraphRuntime(
        registry=registry,
        chat_adapter=MockChatAdapter(),
        config=replace(ProviderConfig(), durable_workflows_enabled=False),
        workflow_service=ForbiddenLegacyService(),
        workflow_graph_host=host,
    )
    request = UserRequest(
        user_id="cutover-user",
        session_id="cutover-session",
        text="research native graph cutover",
        assistant_mode="deep_research",
    )

    state = asyncio.run(runtime.arun_state(request, run_id="cutover-run"))

    assert state.status == "completed"
    assert state.response is not None
    assert state.response.output_refs == ["workflow://workflow-native-cutover"]
    assert state.response.data["workflow"]["execution_engine"] == "langgraph_v3"
    assert len(host.calls) == 1
    assert host.calls[0]["ingress_run_id"] == "cutover-run"

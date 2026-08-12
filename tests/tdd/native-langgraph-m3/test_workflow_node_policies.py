from __future__ import annotations

import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, TimeoutPolicy
from typing_extensions import TypedDict

from assistant_agent.workflows.durable_graph import build_durable_workflow_graph
from assistant_agent.providers.provider_errors import ProviderAdapterError
from assistant_agent.workflows.durable_graph_nodes import (
    WORKFLOW_NODE_TIMEOUT,
    WORKFLOW_TRANSIENT_RETRY_POLICY,
    is_transient_workflow_node_error,
)
from assistant_agent.workflows.transitions import WorkflowTransitionRejected


class _NamedGraph:
    def __init__(self, name: str) -> None:
        self.name = name

    async def __call__(self, _state):
        return {}


def test_retry_classifier_excludes_business_and_permission_failures():
    assert is_transient_workflow_node_error(OSError("temporary"))
    assert is_transient_workflow_node_error(
        NodeTimeoutError("run_worker", 0.01, kind="run", run_timeout=0.01)
    )
    assert not is_transient_workflow_node_error(
        WorkflowTransitionRejected("invalid business plan")
    )
    assert not is_transient_workflow_node_error(PermissionError("denied"))
    assert not is_transient_workflow_node_error(ValueError("state conflict"))
    assert is_transient_workflow_node_error(
        ProviderAdapterError("provider_unavailable", "temporary")
    )
    assert not is_transient_workflow_node_error(
        ProviderAdapterError("provider_permission_denied", "denied")
    )


def test_compiled_worker_and_verifier_nodes_use_native_policies_and_handlers():
    app = build_durable_workflow_graph(
        planning_subgraph=_NamedGraph("WorkflowPlanningSubgraph"),
        worker_branch_subgraph=_NamedGraph("WorkflowWorkerBranch"),
        verifier_branch_subgraph=_NamedGraph("WorkflowVerifierBranch"),
        checkpointer=InMemorySaver(),
    )

    for node_name in ("run_worker", "run_verifier"):
        node = app.nodes[node_name]
        assert node.retry_policy == (WORKFLOW_TRANSIENT_RETRY_POLICY,)
        assert isinstance(node.timeout, TimeoutPolicy)
        assert node.timeout == WORKFLOW_NODE_TIMEOUT
        assert node.error_handler_node == f"__error_handler__{node_name}"
        assert node.error_handler_node in app.nodes
    assert app.builder.nodes["decide_verification"].ends == (
        "prepare_wave",
        "publish",
        "join_wave",
        "fail",
    )


def test_native_timeout_retries_three_attempts_then_runs_error_handler():
    attempts = 0

    class State(TypedDict, total=False):
        outcome: str

    async def slow_worker(_state):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.03)

    def fallback(_state, error: NodeError) -> Command:
        assert isinstance(error.error, NodeTimeoutError)
        return Command(update={"outcome": "workflow_node_timeout"}, goto=END)

    builder = StateGraph(State)
    builder.add_node(
        "run_worker",
        slow_worker,
        retry_policy=WORKFLOW_TRANSIENT_RETRY_POLICY,
        timeout=TimeoutPolicy(run_timeout=0.005),
        error_handler=fallback,
    )
    builder.add_edge(START, "run_worker")
    app = builder.compile(checkpointer=InMemorySaver())
    result = asyncio.run(app.ainvoke({}, config={"configurable": {"thread_id": "policy-timeout"}}))
    assert attempts == 3
    assert result["outcome"] == "workflow_node_timeout"


def test_native_retry_reaches_success_after_two_transient_failures():
    attempts = 0

    class State(TypedDict, total=False):
        outcome: str

    async def transient_worker(_state):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("temporary")
        return {"outcome": "committed-once"}

    builder = StateGraph(State)
    builder.add_node(
        "run_worker",
        transient_worker,
        retry_policy=WORKFLOW_TRANSIENT_RETRY_POLICY,
    )
    builder.add_edge(START, "run_worker")
    builder.add_edge("run_worker", END)
    result = asyncio.run(builder.compile().ainvoke({}))
    assert attempts == 3
    assert result == {"outcome": "committed-once"}


def test_native_retry_does_not_repeat_business_rejection():
    attempts = 0

    class State(TypedDict, total=False):
        outcome: str

    async def rejected_worker(_state):
        nonlocal attempts
        attempts += 1
        raise WorkflowTransitionRejected("invalid admitted plan")

    def fallback(_state, error: NodeError) -> Command:
        assert isinstance(error.error, WorkflowTransitionRejected)
        return Command(update={"outcome": "rejected"}, goto=END)

    builder = StateGraph(State)
    builder.add_node(
        "run_worker",
        rejected_worker,
        retry_policy=WORKFLOW_TRANSIENT_RETRY_POLICY,
        error_handler=fallback,
    )
    builder.add_edge(START, "run_worker")
    result = asyncio.run(builder.compile().ainvoke({}))
    assert attempts == 1
    assert result == {"outcome": "rejected"}

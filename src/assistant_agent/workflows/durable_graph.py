"""The single native StateGraph builder for Durable Workflow v3."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import TimeoutPolicy

from assistant_agent.workflows.durable_graph_nodes import (
    apply_branch_resumes_node,
    await_branch_input_node,
    fail_node,
    decide_verification_node,
    join_wave_node,
    prepare_next_wave_node,
    publish_node,
    route_after_join,
    route_resumed_branches,
    route_next_wave,
    workflow_node_error_handler,
    WORKFLOW_NODE_TIMEOUT,
    WORKFLOW_TRANSIENT_RETRY_POLICY,
)
from assistant_agent.workflows.graph_context import WorkflowGraphRuntimeContext
from assistant_agent.workflows.graph_state import DurableWorkflowState


def build_durable_workflow_graph(
    *,
    planning_subgraph: Any,
    worker_branch_subgraph: Any,
    verifier_branch_subgraph: Any,
    checkpointer: Any,
    store: Any | None = None,
    node_timeout: TimeoutPolicy = WORKFLOW_NODE_TIMEOUT,
) -> Any:
    if getattr(planning_subgraph, "name", None) != "WorkflowPlanningSubgraph":
        raise ValueError("DurableWorkflowGraph requires WorkflowPlanningSubgraph")
    if getattr(worker_branch_subgraph, "name", None) != "WorkflowWorkerBranch":
        raise ValueError("DurableWorkflowGraph requires WorkflowWorkerBranch")
    if getattr(verifier_branch_subgraph, "name", None) != "WorkflowVerifierBranch":
        raise ValueError("DurableWorkflowGraph requires WorkflowVerifierBranch")
    builder = StateGraph(
        DurableWorkflowState,
        context_schema=WorkflowGraphRuntimeContext,
    )
    builder.add_node("workflow_planning", planning_subgraph)
    builder.add_node("prepare_wave", prepare_next_wave_node)
    builder.add_node(
        "run_worker",
        worker_branch_subgraph,
        retry_policy=WORKFLOW_TRANSIENT_RETRY_POLICY,
        timeout=node_timeout,
        error_handler=workflow_node_error_handler,
    )
    builder.add_node(
        "run_verifier",
        verifier_branch_subgraph,
        retry_policy=WORKFLOW_TRANSIENT_RETRY_POLICY,
        timeout=node_timeout,
        error_handler=workflow_node_error_handler,
    )
    builder.add_node("join_wave", join_wave_node)
    builder.add_node("await_branch_input", await_branch_input_node)
    builder.add_node("apply_branch_resumes", apply_branch_resumes_node)
    builder.add_node(
        "decide_verification",
        decide_verification_node,
        destinations=("prepare_wave", "publish", "join_wave", "fail"),
    )
    builder.add_node("publish", publish_node)
    builder.add_node("fail", fail_node)
    builder.add_edge(START, "workflow_planning")
    builder.add_edge("workflow_planning", "prepare_wave")
    builder.add_conditional_edges("prepare_wave", route_next_wave)
    builder.add_edge("run_worker", "join_wave")
    builder.add_edge("run_verifier", "join_wave")
    builder.add_conditional_edges(
        "join_wave",
        route_after_join,
        {
            "next_wave": "prepare_wave",
            "verification": "decide_verification",
            "fail": "fail",
        },
    )
    builder.add_edge("await_branch_input", "apply_branch_resumes")
    builder.add_conditional_edges("apply_branch_resumes", route_resumed_branches)
    builder.add_edge("publish", END)
    builder.add_edge("fail", END)
    return builder.compile(
        checkpointer=checkpointer,
        store=store,
        name="DurableWorkflowGraph",
    )


__all__ = ["build_durable_workflow_graph"]

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from assistant_agent.workflows.graph_state import WorkflowWorkerControl


@pytest.mark.parametrize(
    "control",
    [
        WorkflowWorkerControl(outcome="completed", summary="done"),
        WorkflowWorkerControl(
            outcome="blocked",
            summary="need input",
            required_fields=("topic",),
            prompt_code="need_topic",
            safe_prompt="Which topic should I use?",
        ),
        WorkflowWorkerControl(
            outcome="failed",
            summary="failed",
            error_code="worker_failed",
        ),
    ],
)
def test_worker_control_accepts_only_complete_outcome_shapes(control):
    assert control.outcome in {"completed", "blocked", "failed"}


@pytest.mark.parametrize(
    "payload",
    [
        {"outcome": "completed", "summary": "done", "extra": "bad"},
        {"outcome": "blocked", "summary": "need input"},
        {
            "outcome": "blocked",
            "summary": "need input",
            "required_fields": ["topic"],
            "prompt_code": "need_topic",
            "safe_prompt": "/home/private/secret.txt",
        },
        {"outcome": "failed", "summary": "failed"},
    ],
)
def test_worker_control_rejects_extra_missing_or_unsafe_fields(payload):
    with pytest.raises(ValidationError):
        WorkflowWorkerControl.model_validate(payload)


def test_worker_branch_is_a_static_expandable_compiled_subgraph(tmp_path):
    from workflow_graph_probe import workflow_probe

    app, _context, _initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"a": []}
    )
    try:
        subgraphs = [
            (name, graph.name) for name, graph in app.get_subgraphs(recurse=True)
        ]
        assert ("run_worker", "WorkflowWorkerBranch") in subgraphs
        assert (
            "run_worker|worker_profile",
            "AssistantTurnGraph.worker",
        ) in subgraphs
        assert "run_worker:worker_profile:assistant" in app.get_graph(xray=True).nodes
    finally:
        artifact_store.close()


@pytest.mark.parametrize(
    ("control", "expected_status", "expected_phase"),
    [
        (
            {
                "outcome": "blocked",
                "summary": "need input",
                "required_fields": ["topic"],
                "prompt_code": "need_topic",
                "safe_prompt": "Which topic should I use?",
            },
            "blocked",
            "waiting_input",
        ),
        (
            {
                "outcome": "failed",
                "summary": "worker failed",
                "error_code": "worker_failed",
            },
            "failed",
            "failed",
        ),
    ],
)
def test_worker_branch_projects_control_without_child_interrupt(
    tmp_path,
    control,
    expected_status,
    expected_phase,
):
    from workflow_graph_probe import config, workflow_probe

    response = json.dumps({"workflow_control": control})
    app, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path,
        {"a": []},
        worker_responses={"a": response},
    )
    try:
        final = asyncio.run(app.ainvoke(initial, config=config(), context=context))
        assert final["status"] == expected_status
        assert final["phase"] == expected_phase
        snapshot = asyncio.run(app.aget_state(config()))
        assert snapshot.interrupts == ()
    finally:
        artifact_store.close()

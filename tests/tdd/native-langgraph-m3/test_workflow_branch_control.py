from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from assistant_agent.workflows.graph_state import WorkflowWorkerControl


@pytest.mark.parametrize(
    "control",
    [
        WorkflowWorkerControl(
            outcome="completed",
            summary="done",
            content="deliverable",
            acceptance_evidence=(
                {"criterion_id": "criterion_a", "evidence": "delivered"},
            ),
        ),
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
        {"outcome": "completed", "summary": "done"},
        {"outcome": "blocked", "summary": "need input"},
        {
            "outcome": "blocked",
            "summary": "need input",
            "content": "not allowed",
            "required_fields": ["topic"],
            "prompt_code": "need_topic",
            "safe_prompt": "Which topic should I use?",
        },
        {
            "outcome": "blocked",
            "summary": "need input",
            "required_fields": ["topic"],
            "prompt_code": "need_topic",
            "safe_prompt": "/home/private/secret.txt",
        },
        {"outcome": "failed", "summary": "failed"},
        {
            "outcome": "failed",
            "summary": "failed",
            "content": "not allowed",
            "error_code": "worker_failed",
        },
    ],
)
def test_worker_control_rejects_extra_missing_or_unsafe_fields(payload):
    with pytest.raises(ValidationError):
        WorkflowWorkerControl.model_validate(payload)


def test_worker_completed_content_fits_profile_response_transport():
    control = WorkflowWorkerControl(
        outcome="completed",
        summary="s" * 4_000,
        content="c" * 27_000,
        acceptance_evidence=(
            {"criterion_id": "criterion_a", "evidence": "delivered"},
        ),
    )
    envelope = json.dumps(
        {"workflow_control": control.model_dump(mode="json")},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert len(envelope) < 32_000
    with pytest.raises(ValidationError):
        WorkflowWorkerControl(
            outcome="completed",
            summary="done",
            content="c" * 27_001,
            acceptance_evidence=(
                {"criterion_id": "criterion_a", "evidence": "delivered"},
            ),
        )


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
        if expected_status == "blocked":
            assert snapshot.tasks
            assert all(task.name == "await_branch_input" for task in snapshot.tasks)
            assert all(task.interrupts for task in snapshot.tasks)
        else:
            assert snapshot.interrupts == ()
    finally:
        artifact_store.close()


def test_plain_text_worker_output_fails_closed_without_artifact(tmp_path):
    from assistant_agent.workflows.graph_state import latest_results
    from workflow_graph_probe import config, workflow_probe

    app, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path,
        {"a": []},
        worker_responses={"a": "unstructured completion"},
    )
    try:
        final = asyncio.run(app.ainvoke(initial, config=config(), context=context))
        result = latest_results(
            final["result_ledger"], final["execution_generation_by_node"]
        )["a"]
        assert final["status"] == "failed"
        assert result.status == "failed"
        assert result.error_code == "workflow_worker_control_invalid"
        assert result.artifact_refs == ()
    finally:
        artifact_store.close()


def test_native_worker_request_uses_strict_work_item_control_prompt(tmp_path):
    from workflow_graph_probe import config, workflow_probe

    app, context, initial, worker, artifact_store = workflow_probe(
        tmp_path,
        {"a": []},
    )
    try:
        asyncio.run(app.ainvoke(initial, config=config(), context=context))

        assert len(worker.requests) == 1
        request_text = worker.requests[0].user_query
        assert worker.requests[0].response_format == {"type": "json_object"}
        assert worker.requests[0].max_tokens == 8_192
        assert "workflow_control" in request_text
        assert '"outcome"' in request_text
        assert '"completed"' in request_text
        assert '"content"' in request_text
        assert '"status"' not in request_text
        assert "criterion_a" in request_text
        assert "execute a" in request_text
        assert len(request_text) <= 32_000
    finally:
        artifact_store.close()


def test_native_completed_control_projects_envelope_content(tmp_path):
    from assistant_agent.identity import RequestIdentity
    from workflow_graph_probe import config, workflow_probe

    response = json.dumps(
        {
            "workflow_control": {
                "outcome": "completed",
                "summary": "done",
                "content": "bounded deliverable",
                "acceptance_evidence": [
                    {"criterion_id": "criterion_a", "evidence": "delivered"}
                ],
            }
        }
    )
    app, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path,
        {"a": []},
        worker_responses={"a": response},
    )
    try:
        final = asyncio.run(app.ainvoke(initial, config=config(), context=context))
        child = final["result_ledger"]
        assert child
        result_ref = next(iter(final["result_artifact_refs"]))
        assert artifact_store.read_text(
            identity=RequestIdentity.for_user(
                user_id="user-send",
                agent_id="agent-send",
                session_id="session-send",
            ),
            artifact_ref=result_ref,
        ) == "bounded deliverable"
    finally:
        artifact_store.close()


def test_completed_control_without_content_fails_closed(tmp_path):
    from assistant_agent.workflows.graph_state import latest_results
    from workflow_graph_probe import config, workflow_probe

    response = json.dumps(
        {"workflow_control": {"outcome": "completed", "summary": "done"}}
    )
    app, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path,
        {"a": []},
        worker_responses={"a": response},
    )
    try:
        final = asyncio.run(app.ainvoke(initial, config=config(), context=context))
        result = latest_results(
            final["result_ledger"], final["execution_generation_by_node"]
        )["a"]
        assert final["status"] == "failed"
        assert result.status == "failed"
        assert result.error_code == "workflow_worker_control_invalid"
        assert result.artifact_refs == ()
        assert tuple(final["result_artifact_refs"]) == ()
    finally:
        artifact_store.close()


def test_legacy_status_alias_is_not_a_valid_worker_control(tmp_path):
    from assistant_agent.workflows.graph_state import latest_results
    from workflow_graph_probe import config, workflow_probe

    response = json.dumps(
        {"workflow_control": {"status": "succeeded", "summary": "done"}}
    )
    app, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path,
        {"a": []},
        worker_responses={"a": response},
    )
    try:
        final = asyncio.run(app.ainvoke(initial, config=config(), context=context))
        result = latest_results(
            final["result_ledger"], final["execution_generation_by_node"]
        )["a"]
        assert final["status"] == "failed"
        assert result.error_code == "workflow_worker_control_invalid"
    finally:
        artifact_store.close()


def test_deep_research_worker_does_not_inherit_registered_read_tools(tmp_path):
    from workflow_graph_probe import config, workflow_probe

    app, context, initial, worker, artifact_store = workflow_probe(
        tmp_path,
        {"a": []},
        worker_responses={
            "a": json.dumps(
                {"workflow_control": {"outcome": "completed", "summary": "done"}}
            )
        },
    )
    try:
        final = asyncio.run(app.ainvoke(initial, config=config(), context=context))
        assert final["status"] == "failed"
        assert worker.requests[0].tools == []
        assert tuple(final["active_wave"]) == ()
    finally:
        artifact_store.close()


def test_native_verifier_request_has_strict_schema_constraints_and_repair_scope(
    tmp_path,
):
    from workflow_graph_probe import config, proposal, workflow_probe

    plan = proposal({"a": [], "verify": ["a"]})
    plan["constraint_bindings"] = [
        {
            "constraint_id": "required_evidence",
            "statement": "cite evidence",
            "owner_node_ids": ["a"],
            "verifier_node_id": "verify",
            "severity": "required",
        }
    ]
    app, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path,
        {"a": [], "verify": ["a"]},
        plan_payload=plan,
    )
    try:
        final = asyncio.run(app.ainvoke(initial, config=config(), context=context))
        verifier = context.services.provider_registry["verifier"]

        assert final["status"] == "completed"
        assert len(verifier.requests) == 1
        request_text = verifier.requests[0].user_query
        assert "workflow_verification" in request_text
        assert '"verified"' in request_text
        assert '"repair_node_ids"' in request_text
        assert "cite evidence" in request_text
        assert '"repair_candidate_ids": ["a"]' in request_text
        assert len(request_text) <= 32_000
    finally:
        artifact_store.close()


def test_native_verifier_prompt_bounds_large_ascii_artifact(tmp_path):
    from workflow_graph_probe import config, proposal, workflow_probe

    plan = proposal({"a": [], "verify": ["a"]})
    plan["constraint_bindings"] = [
        {
            "constraint_id": "required_evidence",
            "statement": "cite evidence",
            "owner_node_ids": ["a"],
            "verifier_node_id": "verify",
            "severity": "required",
        }
    ]
    response = json.dumps(
        {
            "workflow_control": {
                "outcome": "completed",
                "summary": "done",
                "content": "a" * 20_000,
                "acceptance_evidence": [
                    {"criterion_id": "criterion_a", "evidence": "delivered"}
                ],
            }
        }
    )
    app, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path,
        {"a": [], "verify": ["a"]},
        plan_payload=plan,
        worker_responses={"a": response},
    )
    try:
        final = asyncio.run(app.ainvoke(initial, config=config(), context=context))
        verifier = context.services.provider_registry["verifier"]

        assert final["status"] == "completed"
        request_text = verifier.requests[0].user_query
        assert len(request_text) <= 32_000
        assert '"repair_candidate_ids": ["a"]' in request_text
        assert '"trimmed": true' in request_text
    finally:
        artifact_store.close()

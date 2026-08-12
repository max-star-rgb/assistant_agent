from __future__ import annotations

import asyncio

from langgraph.types import Command

from assistant_agent.workflows.durable_graph_nodes import (
    decide_verification_node,
    minimal_repair_closure,
)
from assistant_agent.workflows.graph_state import (
    PersistedAdmittedWorkflowPlan,
    WorkflowBranchResult,
    latest_results,
    ledger_update,
    merge_result_ledger,
)
from workflow_graph_probe import acceptance, config, workflow_probe


def _acceptance() -> dict[str, object]:
    return {
        "schema_version": "workflow_step_acceptance_v2",
        "output": {"artifact_type": "research_report", "description": "evidence"},
        "criteria": ({"criterion_id": "criterion", "statement": "exists"},),
    }


def _plan() -> PersistedAdmittedWorkflowPlan:
    dependencies = {
        "a": (),
        "b": (),
        "synthesize": ("a", "b"),
        "verify": ("synthesize",),
    }
    return PersistedAdmittedWorkflowPlan.model_validate(
        {
            "workflow_id": "wf-repair",
            "version": 2,
            "definition_version": "3",
            "revision_reason": "admitted",
            "nodes": tuple(
                {
                    "node_id": node_id,
                    "kind": "verify" if node_id == "verify" else "research",
                    "display_title": node_id,
                    "objective": node_id,
                    "depends_on": parents,
                    "acceptance_contract": _acceptance(),
                }
                for node_id, parents in dependencies.items()
            ),
            "constraint_bindings": (
                {
                    "constraint_id": "required-source",
                    "statement": "must cite sources",
                    "owner_node_ids": ("synthesize",),
                    "verifier_node_id": "verify",
                    "severity": "required",
                },
            ),
            "deliverable_bindings": (
                {"deliverable": "report", "producer_node_id": "synthesize"}
            ,),
            "created_at": "2026-08-12T00:00:00+00:00",
        }
    )


def _result(
    node_id: str,
    *,
    generation: int = 0,
    status: str = "succeeded",
    repair_node_ids: tuple[str, ...] = (),
) -> WorkflowBranchResult:
    return WorkflowBranchResult(
        node_id=node_id,
        execution_generation=generation,
        profile="verifier" if node_id == "verify" else "worker",
        status=status,
        summary=status,
        repair_node_ids=repair_node_ids,
        artifact_refs=(f"artifact://{node_id}-g{generation}",)
        if node_id == "synthesize"
        else (),
    )


def _state(verifier: WorkflowBranchResult) -> dict[str, object]:
    ledger: dict[str, object] = {}
    for result in (_result("a"), _result("b"), _result("synthesize"), verifier):
        ledger = merge_result_ledger(ledger, ledger_update(result))
    return {
        "admitted_plan": _plan(),
        "execution_generation_by_node": {"a": 0, "b": 0, "synthesize": 0, "verify": 0},
        "result_ledger": ledger,
        "repair_round": 0,
        "budget": {
            "model_calls_remaining": 20,
            "tool_calls_remaining": 20,
            "workflow_quanta_remaining": 20,
            "deadline_at": "2026-08-13T00:00:00+00:00",
        },
        "status": "running",
        "phase": "verifying",
        "active_wave": (),
        "result_artifact_refs": ("artifact://stale",),
        "errors": (),
    }


def test_minimal_repair_only_invalidates_requested_ancestors_and_their_descendants():
    plan = _plan()
    assert minimal_repair_closure(plan, ("a",), "verify") == frozenset(
        {"a", "synthesize", "verify"}
    )

    command = decide_verification_node(
        _state(_result("verify", status="repair", repair_node_ids=("a",)))
    )

    assert isinstance(command, Command)
    assert command.goto == "prepare_wave"
    assert command.update["execution_generation_by_node"] == {
        "a": 1,
        "b": 0,
        "synthesize": 1,
        "verify": 1,
    }
    current = latest_results(
        command.update.get("result_ledger", _state(_result("verify"))["result_ledger"]),
        command.update["execution_generation_by_node"],
    )
    assert set(current) == {"b"}


def test_invalid_repair_scope_fails_closed_without_invalidating_the_dag():
    state = _state(_result("verify", status="repair", repair_node_ids=("b", "unknown")))
    command = decide_verification_node(state)

    assert command.goto == "fail"
    assert "execution_generation_by_node" not in command.update
    assert command.update["errors"][0].code == "invalid_repair_scope"


def test_empty_and_exhausted_repair_requests_fail_closed():
    empty = decide_verification_node(
        _state(_result("verify", status="repair", repair_node_ids=()))
    )
    assert empty.goto == "fail"
    assert empty.update["errors"][0].code == "invalid_repair_scope"

    state = _state(_result("verify", status="repair", repair_node_ids=("a",)))
    state["repair_round"] = 3
    exhausted = decide_verification_node(state)
    assert exhausted.goto == "fail"
    assert exhausted.update["errors"][0].code == "repair_budget_exhausted"


def test_verified_result_routes_to_publish_with_only_current_deliverable_refs():
    command = decide_verification_node(_state(_result("verify")))
    assert command.goto == "publish"
    assert command.update["result_artifact_refs"] == ("artifact://synthesize-g0",)


def test_compiled_verifier_subgraph_repairs_only_affected_branch(tmp_path):
    dependencies = {
        "a": [],
        "b": [],
        "synthesize": ["a", "b"],
        "verify": ["synthesize"],
    }
    payload = {
        "schema_version": "workflow_plan_v2",
        "nodes": [
            {
                "node_id": node_id,
                "display_title": node_id,
                "objective": f"execute {node_id}",
                "depends_on": parents,
                "acceptance_contract": acceptance(f"criterion_{node_id}"),
            }
            for node_id, parents in dependencies.items()
        ],
        "deliverable_bindings": [
            {"deliverable": "report", "producer_node_id": "verify"}
        ],
        "constraint_bindings": [
            {
                "constraint_id": "required-source",
                "statement": "must cite sources",
                "owner_node_ids": ["synthesize"],
                "verifier_node_id": "verify",
                "severity": "required",
            }
        ],
    }
    app, context, initial, worker, artifact_store = workflow_probe(
        tmp_path,
        dependencies,
        plan_payload=payload,
        verifier_responses=[
            {"status": "repair", "summary": "repair a", "repair_node_ids": ["a"]},
            {"status": "verified", "summary": "verified"},
        ],
    )

    async def execute():
        events = [
            item
            async for item in app.astream(
                initial,
                config=config(),
                context=context,
                stream_mode=["updates", "tasks"],
                subgraphs=True,
                version="v2",
            )
        ]
        return events, await app.aget_state(config())

    try:
        events, snapshot = asyncio.run(execute())
        final = snapshot.values
        assert final["status"] == "completed"
        assert final["execution_generation_by_node"] == {
            "a": 1,
            "b": 0,
            "synthesize": 1,
            "verify": 1,
        }
        worker_queries = [request.user_query for request in worker.requests]
        assert sum("execute a" in query for query in worker_queries) == 2
        assert sum("execute b" in query for query in worker_queries) == 1
        assert sum("execute synthesize" in query for query in worker_queries) == 2
        verifier = context.services.provider_registry["verifier"]
        assert len(verifier.requests) == 2
        assert any(
            "verifier_profile:" in "/".join(item.get("ns") or ())
            for item in events
        )
        assert latest_results(
            final["result_ledger"], final["execution_generation_by_node"]
        )["verify"].status == "succeeded"
    finally:
        artifact_store.close()

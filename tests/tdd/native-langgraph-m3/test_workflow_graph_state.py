from __future__ import annotations

import asyncio
import functools
import inspect
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.context.service import ContextService
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp
from assistant_agent.runtime.assistant_graph_profiles import (
    ProfileInvocationInput,
    profile_input_adapter,
    profile_output_adapter,
)
from assistant_agent.runtime.chat_adapter import MockChatAdapter
from assistant_agent.runtime.state import AgentError
from assistant_agent.runtime.tool_operation_barrier import SQLiteToolOperationStore
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.workflows.graph_context import (
    BranchProfileContextFactory,
    WorkflowGraphRuntimeServices,
)
from assistant_agent.workflows.graph_state import (
    PersistedWorkflowBudgetSlice,
    PersistedWorkflowIdentity,
    PersistedWorkflowStepAcceptanceContract,
    WorkflowBranchResult,
    WorkflowGraphStateConflict,
    WorkflowProfileAssignment,
    WorkflowResultConflict,
    initial_workflow_graph_state,
    latest_results,
    ledger_update,
    merge_graph_errors,
    merge_result_ledger,
    merge_resume_values,
    merge_sorted_unique_refs,
    result_conflicts,
    validate_durable_workflow_state,
)
from assistant_agent.workflows.models import (
    WorkflowBudget,
    WorkflowPlanVersion,
    WorkflowRecord,
    WorkflowSubmission,
)
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.workflows.store import InMemoryWorkflowStore
from tests.core.support import ProbeTool


def _submission() -> WorkflowSubmission:
    return WorkflowSubmission(
        workflow_type="deep_research",
        objective="compare native graph runtimes",
        deliverables=["research report"],
        constraints=["cite evidence"],
        inputs={"research_questions": ["How does recovery work?"]},
        requested_budget={
            "model_calls": 12,
            "tool_calls": 8,
            "workflow_quanta": 32,
            "deadline_seconds": 3600,
        },
        durability_reasons=["multi_stage"],
        seed_artifact_refs=["artifact://seed/evidence"],
        idempotency_key="workflow-submission-1",
    )


def _budget() -> WorkflowBudget:
    return WorkflowBudget(
        model_calls_remaining=12,
        tool_calls_remaining=8,
        workflow_quanta_remaining=32,
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _record(*, execution_engine: str | None = "langgraph_v3") -> WorkflowRecord:
    payload = {
        "workflow_id": "wf-1",
        "workflow_type": "deep_research",
        "definition_version": "3",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "session_id": "session-1",
        "ingress_run_id": "ingress-run-1",
        "idempotency_key": "workflow-submission-1",
        "submission_digest": "a" * 64,
        "objective": "compare native graph runtimes",
        "deliverables": ["research report"],
        "constraints": ["cite evidence"],
        "inputs": {"research_questions": ["How does recovery work?"]},
        "phase": "planning",
        "budget": _budget().model_dump(mode="python"),
        "seed_artifact_refs": ["artifact://seed/evidence"],
    }
    if execution_engine is not None:
        payload["execution_engine"] = execution_engine
    return WorkflowRecord.model_validate(payload)


def _acceptance() -> PersistedWorkflowStepAcceptanceContract:
    return PersistedWorkflowStepAcceptanceContract.model_validate_json(
        json.dumps(
            {
                "schema_version": "workflow_step_acceptance_v2",
                "output": {
                    "artifact_type": "research_report",
                    "description": "A bounded evidence report",
                },
                "criteria": [
                    {
                        "criterion_id": "evidence",
                        "statement": "Every claim has evidence",
                    }
                ],
            }
        )
    )


def _plan() -> WorkflowPlanVersion:
    return WorkflowPlanVersion.model_validate(
        {
            "workflow_id": "wf-1",
            "version": 2,
            "definition_version": "3",
            "revision_reason": "runtime_planner",
            "work_items": [
                {
                    "work_item_id": "research",
                    "kind": "agent",
                    "display_title": "Research",
                    "objective": "Collect evidence",
                    "acceptance_contract": _acceptance().model_dump(mode="python"),
                }
            ],
            "deliverable_bindings": [
                {
                    "deliverable": "research report",
                    "producer_work_item_id": "research",
                }
            ],
        }
    )


def _result(node_id: str, generation: int, summary: str) -> WorkflowBranchResult:
    return WorkflowBranchResult(
        node_id=node_id,
        execution_generation=generation,
        profile="worker",
        status="succeeded",
        summary=summary,
        artifact_refs=(f"artifact://{node_id}/{generation}",),
    )


def _canonical_ledger(value: object) -> str:
    return json.dumps(
        {
            key: (slot.model_dump(mode="json") if hasattr(slot, "model_dump") else slot)
            for key, slot in sorted(value.items())
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _assignment(
    profile: str = "worker",
    *,
    registry_generation: str = "sha256:" + "a" * 64,
    node_id: str = "research",
    capability_refs: tuple[str, ...] = (),
    available_tool_names: tuple[str, ...] = (),
    explicit_tool_allowlist: tuple[str, ...] = (),
    model_calls: int = 2,
    tool_calls: int = 0,
) -> WorkflowProfileAssignment:
    return WorkflowProfileAssignment.create(
        profile=profile,
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        workflow_id="wf-1",
        workflow_thread_id="workflow-thread-1",
        node_id=node_id,
        execution_generation=0,
        run_id=f"run-{profile}-{node_id}",
        trace_id=f"trace-{profile}-{node_id}",
        objective="Collect evidence",
        constraints=("cite evidence",),
        input_artifact_refs=("artifact://seed/evidence",),
        acceptance_contract=_acceptance(),
        capability_refs=capability_refs,
        explicit_tool_allowlist=explicit_tool_allowlist,
        available_tool_names=available_tool_names,
        tool_scope_ref=registry_generation,
        budget_slice=PersistedWorkflowBudgetSlice(
            model_calls=model_calls,
            tool_calls=tool_calls,
            workflow_quanta=4,
        ),
    )


def _child_state(
    assignment: WorkflowProfileAssignment,
    registry: ToolRegistry,
):
    return profile_input_adapter(
        {
            "user_id": assignment.user_id,
            "session_id": assignment.session_id,
            "agent_id": assignment.agent_id,
            "run_id": assignment.run_id,
            "trace_id": assignment.trace_id,
            "registered_tool_specs": registry.list_specs(),
            "available_tool_names": list(assignment.available_tool_names),
        },
        ProfileInvocationInput(
            profile=assignment.profile,
            assignment_ref=assignment.assignment_ref,
            objective=assignment.objective,
            constraints=assignment.constraints,
            capability_refs=assignment.capability_refs,
            explicit_tool_allowlist=assignment.explicit_tool_allowlist,
        ),
        model_call_limit=assignment.budget_slice.model_calls,
        tool_call_limit=assignment.budget_slice.tool_calls,
    )


def _probe_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ProbeTool())
    registry.seal()
    return registry


def _services(tmp_path, registry: ToolRegistry) -> WorkflowGraphRuntimeServices:
    return WorkflowGraphRuntimeServices(
        provider_registry={
            "planner": MockChatAdapter(),
            "worker": MockChatAdapter(),
            "verifier": MockChatAdapter(),
        },
        tool_registry=registry,
        context_service=ContextService(),
        operation_store=SQLiteToolOperationStore(tmp_path / "operations.sqlite3"),
        memory_host=object(),
        workflow_identity=PersistedWorkflowIdentity(
            user_id="user-1",
            session_id="session-1",
            agent_id="agent-1",
            workflow_thread_id="workflow-thread-1",
            turn_origin_id="ingress-run-1",
        ),
        cancel_reader=lambda _assignment: None,
        stream_writer=lambda _assignment, _fact: None,
    )


def test_result_ledger_reducer_is_associative_commutative_and_idempotent() -> None:
    a0 = _result("a", 0, "a0")
    b0 = _result("b", 0, "b0")
    conflicting_a0 = _result("a", 0, "conflict")
    updates = [ledger_update(a0), ledger_update(b0), ledger_update(conflicting_a0)]

    outcomes = {
        _canonical_ledger(functools.reduce(merge_result_ledger, order, {}))
        for order in itertools.permutations(updates)
    }
    left_grouped = merge_result_ledger(
        merge_result_ledger(updates[0], updates[1]), updates[2]
    )
    right_grouped = merge_result_ledger(
        updates[0], merge_result_ledger(updates[1], updates[2])
    )

    assert len(outcomes) == 1
    assert left_grouped == right_grouped
    assert merge_result_ledger(left_grouped, left_grouped) == left_grouped
    assert result_conflicts(left_grouped) == (
        WorkflowResultConflict(
            node_id="a",
            execution_generation=0,
            variant_digests=tuple(
                sorted(
                    left_grouped[
                        next(key for key in left_grouped if key.startswith("a"))
                    ]["variants_by_digest"]
                )
            ),
        ),
    )
    with pytest.raises(WorkflowGraphStateConflict, match="a.*generation 0"):
        latest_results(left_grouped, {"a": 0, "b": 0})


def test_latest_results_uses_generation_without_deleting_history() -> None:
    a0 = _result("a", 0, "old")
    a1 = _result("a", 1, "repaired")
    ledger = merge_result_ledger(ledger_update(a0), ledger_update(a1))

    assert latest_results(ledger, {"a": 1}) == {"a": a1}
    assert len(ledger) == 2


def test_other_state_reducers_are_deterministic_and_replay_safe() -> None:
    resume = {
        "action-a": {
            "action_ref": "action-a",
            "fields": [{"name": "answer", "value": "A"}],
        }
    }
    error = {
        "code": "probe_error",
        "message": "safe summary",
        "node_id": "a",
        "execution_generation": 0,
    }

    assert merge_sorted_unique_refs(("b", "a"), ("c", "a")) == ("a", "b", "c")
    assert merge_resume_values(resume, resume) == merge_resume_values({}, resume)
    assert merge_graph_errors((error,), (error,)) == merge_graph_errors((), (error,))


def test_workflow_record_migrates_missing_engine_only_to_legacy() -> None:
    legacy = _record(execution_engine=None)
    graph = _record(execution_engine="langgraph_v3")

    assert legacy.execution_engine == "legacy_scheduler_v2"
    assert graph.execution_engine == "langgraph_v3"
    assert legacy.model_dump(mode="json")["execution_engine"] == "legacy_scheduler_v2"


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    ("record_engine", "allowed_types"),
    [
        ("langgraph_v3", frozenset({"deep_research"})),
        ("legacy_scheduler_v2", frozenset({"long_horizon"})),
    ],
)
def test_legacy_claim_filters_engine_and_workflow_type_inside_store_boundary(
    tmp_path,
    store_kind,
    record_engine,
    allowed_types,
) -> None:
    store = (
        InMemoryWorkflowStore()
        if store_kind == "memory"
        else SQLiteWorkflowStore(tmp_path / "workflows.sqlite3")
    )
    service = WorkflowService(
        store=store,
        definitions=default_workflow_definitions(),
    )
    created = service.submit(
        identity=RequestIdentity.for_user(
            user_id="user-1",
            agent_id="agent-1",
            session_id="session-1",
        ),
        ingress_run_id="ingress-run-1",
        submission=_submission(),
    )
    changed = created.model_copy(deep=True)
    changed.workflow.execution_engine = record_engine
    store.save(changed, expected_revision=created.workflow.revision, events=[])

    claimed = store.claim_ready_work_item(
        worker_id="legacy-worker-1",
        now=datetime.now(timezone.utc),
        lease_seconds=30,
        model_call_limit=2,
        tool_call_limit=0,
        allowed_execution_engines=frozenset({"legacy_scheduler_v2"}),
        allowed_workflow_types=allowed_types,
    )

    assert claimed is None
    loaded = store.load(created.workflow.workflow_id)
    assert loaded is not None
    assert loaded.current_plan.work_items[0].status == "ready"
    store.close()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_claim_scope_requires_explicit_engine_and_workflow_type_allowlists(
    tmp_path,
    store_kind,
) -> None:
    store = (
        InMemoryWorkflowStore()
        if store_kind == "memory"
        else SQLiteWorkflowStore(tmp_path / "workflows.sqlite3")
    )
    signature = inspect.signature(store.claim_ready_work_item)

    assert (
        signature.parameters["allowed_execution_engines"].default
        is inspect.Parameter.empty
    )
    assert (
        signature.parameters["allowed_workflow_types"].default
        is inspect.Parameter.empty
    )
    common = {
        "worker_id": "worker-1",
        "now": datetime.now(timezone.utc),
        "lease_seconds": 30,
        "model_call_limit": 1,
        "tool_call_limit": 0,
    }
    with pytest.raises(ValueError, match="allowlists.*non-empty"):
        store.claim_ready_work_item(
            **common,
            allowed_execution_engines=frozenset(),
            allowed_workflow_types=frozenset({"deep_research"}),
        )
    with pytest.raises(ValueError, match="allowlists.*non-empty"):
        store.claim_ready_work_item(
            **common,
            allowed_execution_engines=frozenset({"legacy_scheduler_v2"}),
            allowed_workflow_types=frozenset(),
        )
    store.close()


def test_initial_state_rejects_legacy_record_and_preserves_strict_identity() -> None:
    with pytest.raises(ValueError, match="legacy_scheduler_v2"):
        initial_workflow_graph_state(
            workflow=_record(execution_engine=None),
            submission=_submission(),
            admitted_plan=None,
            workflow_thread_id="workflow-thread-1",
            invocation_run_id="workflow-run-1",
            invocation_trace_id="workflow-trace-1",
        )

    state = initial_workflow_graph_state(
        workflow=_record(),
        submission=_submission(),
        admitted_plan=_plan(),
        workflow_thread_id="workflow-thread-1",
        invocation_run_id="workflow-run-1",
        invocation_trace_id="workflow-trace-1",
    )

    assert state["graph_name"] == "DurableWorkflowGraph"
    assert state["graph_version"] == "3"
    assert state["state_schema_version"] == 1
    assert state["execution_engine"] == "langgraph_v3"
    assert state["workflow_id"] == "wf-1"
    assert state["submission"]["inputs"] == {
        "schema_version": "deep_research_inputs_v2",
        "research_questions": ["How does recovery work?"],
    }
    assert validate_durable_workflow_state(state) == state


@pytest.mark.parametrize(
    "mutation",
    [
        {"provider_client": object()},
        {"tool_registry": object()},
        {"db_connection": object()},
        {"event_sink": lambda _value: None},
        {"cancel_token": object()},
        {"api_token": "secret-value"},
    ],
)
def test_strict_state_rejects_runtime_objects_and_unknown_channels(mutation) -> None:
    state = initial_workflow_graph_state(
        workflow=_record(),
        submission=_submission(),
        admitted_plan=None,
        workflow_thread_id="workflow-thread-1",
        invocation_run_id="workflow-run-1",
        invocation_trace_id="workflow-trace-1",
    )

    with pytest.raises(ValueError):
        validate_durable_workflow_state({**state, **mutation})


def test_checkpoint_models_reject_unbounded_inputs_and_unsafe_artifact_paths() -> None:
    invalid_submission = _submission().model_copy(
        update={"inputs": {"arbitrary": {"nested": "escape hatch"}}}
    )
    with pytest.raises((ValueError, ValidationError), match="research_questions"):
        initial_workflow_graph_state(
            workflow=_record(),
            submission=invalid_submission,
            admitted_plan=None,
            workflow_thread_id="workflow-thread-1",
            invocation_run_id="workflow-run-1",
            invocation_trace_id="workflow-trace-1",
        )

    invalid_ref_submission = _submission().model_copy(
        update={"seed_artifact_refs": ["/home/user/private.txt"]}
    )
    with pytest.raises((ValueError, ValidationError), match="artifact"):
        initial_workflow_graph_state(
            workflow=_record(),
            submission=invalid_ref_submission,
            admitted_plan=None,
            workflow_thread_id="workflow-thread-1",
            invocation_run_id="workflow-run-1",
            invocation_trace_id="workflow-trace-1",
        )


def test_assignment_is_checkpoint_safe_and_detects_tampering() -> None:
    assignment = _assignment()
    round_trip = WorkflowProfileAssignment.model_validate_json(
        assignment.model_dump_json()
    )
    assert round_trip == assignment

    with pytest.raises(ValidationError, match="assignment_ref"):
        WorkflowProfileAssignment.model_validate(
            {**assignment.model_dump(mode="python"), "objective": "tampered"}
        )


def test_branch_factory_builds_independent_agent_state_executor_and_counters(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    registry.seal()
    assignment = _assignment(registry_generation=registry.generation or "")
    child = _child_state(assignment, registry)
    services = _services(tmp_path, registry)
    factory = BranchProfileContextFactory()

    first = factory.context_for_assignment(assignment, child, services)
    second = factory.context_for_assignment(assignment, child, services)

    assert first is not second
    assert first.agent_state is not second.agent_state
    assert first.tool_executor is not second.tool_executor
    assert (
        first.tool_executor.context_metadata
        is not second.tool_executor.context_metadata
    )
    assert first.agent_state is not None and second.agent_state is not None
    first.agent_state.errors.append(AgentError(message="branch-only-error"))
    assert second.agent_state.errors == []


def test_branch_budget_slice_caps_child_even_with_nonempty_tool_catalog(
    tmp_path,
) -> None:
    registry = _probe_registry()
    assignment = _assignment(
        registry_generation=registry.generation or "",
        available_tool_names=("probe_tool",),
        explicit_tool_allowlist=("probe_tool",),
        model_calls=1,
        tool_calls=0,
    )
    child = _child_state(assignment, registry)

    assert child["catalog"]["available_tool_names"] == ["probe_tool"]
    assert child["max_assistant_iterations"] == 1
    assert child["max_tool_calls_per_run"] == 0
    assert child["max_action_tool_calls_per_run"] == 0

    over_budget = json.loads(json.dumps(child))
    over_budget["max_tool_calls_per_run"] = 1
    over_budget["max_action_tool_calls_per_run"] = 1
    with pytest.raises(ValueError, match="budget"):
        BranchProfileContextFactory().context_for_assignment(
            assignment,
            over_budget,
            _services(tmp_path, registry),
        )


@pytest.mark.parametrize("profile", ["planner", "worker", "verifier"])
def test_fresh_factory_rebuild_matches_uninterrupted_profile_output(
    tmp_path,
    profile,
) -> None:
    registry = _probe_registry()
    available_tool_names = () if profile == "planner" else ("probe_tool",)
    assignment = _assignment(
        profile,
        registry_generation=registry.generation or "",
        node_id=f"{profile}-node",
        available_tool_names=available_tool_names,
        explicit_tool_allowlist=available_tool_names,
    )
    child = _child_state(assignment, registry)
    first_app = AssistantTurnGraphApp()

    first_context = BranchProfileContextFactory().context_for_assignment(
        assignment,
        child,
        _services(tmp_path / "first", registry),
    )
    first = asyncio.run(
        first_app.graph_for_profile(profile).ainvoke(child, context=first_context)
    )

    fresh_registry = _probe_registry()
    fresh_child = json.loads(json.dumps(child))
    fresh_app = AssistantTurnGraphApp()
    fresh_context = BranchProfileContextFactory().context_for_assignment(
        WorkflowProfileAssignment.model_validate_json(assignment.model_dump_json()),
        fresh_child,
        _services(tmp_path / "fresh", fresh_registry),
    )
    recovered = asyncio.run(
        fresh_app.graph_for_profile(profile).ainvoke(
            fresh_child,
            context=fresh_context,
        )
    )

    assert fresh_registry is not registry
    assert fresh_registry.generation == registry.generation
    assert fresh_app is not first_app
    assert fresh_context.tool_executor is not first_context.tool_executor
    assert fresh_context.agent_state is not first_context.agent_state
    assert fresh_context.chat_adapter is not first_context.chat_adapter
    assert fresh_context.chat_adapter.provider == first_context.chat_adapter.provider
    assert fresh_context.profile_allowed_tool_names == frozenset(available_tool_names)
    recovered_output = profile_output_adapter(recovered)
    uninterrupted_output = profile_output_adapter(first)
    assert recovered_output.tool_trajectory == uninterrupted_output.tool_trajectory
    assert recovered_output == uninterrupted_output


def test_branch_factory_fails_before_child_node_on_owner_or_scope_mismatch(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    registry.seal()
    assignment = _assignment(registry_generation=registry.generation or "")
    child = _child_state(assignment, registry)
    services = _services(tmp_path, registry)
    factory = BranchProfileContextFactory()

    mismatched_owner = json.loads(json.dumps(child))
    mismatched_owner["request"]["user_id"] = "other-user"
    with pytest.raises(ValueError, match="owner|identity"):
        factory.context_for_assignment(assignment, mismatched_owner, services)

    stale_scope = WorkflowProfileAssignment.create(
        **{
            **assignment.model_dump(
                mode="python",
                exclude={"assignment_ref", "tool_scope_ref"},
            ),
            "tool_scope_ref": "sha256:" + "f" * 64,
        }
    )
    stale_child = _child_state(stale_scope, registry)
    with pytest.raises(ValueError, match="Tool scope"):
        factory.context_for_assignment(stale_scope, stale_child, services)

    mismatched_capability_refs = json.loads(json.dumps(child))
    mismatched_capability_refs["capability_refs"] = ["capability-other"]
    with pytest.raises(ValueError, match="capability refs"):
        factory.context_for_assignment(
            assignment,
            mismatched_capability_refs,
            services,
        )

    mismatched_assignment_ref = json.loads(json.dumps(child))
    mismatched_assignment_ref["context_refs"][0]["ref"] = (
        "workflow-assignment:sha256:" + "0" * 64
    )
    with pytest.raises(ValueError, match="assignment reference"):
        factory.context_for_assignment(
            assignment,
            mismatched_assignment_ref,
            services,
        )

    narrowed_assignment = WorkflowProfileAssignment.create(
        **{
            **assignment.model_dump(
                mode="python",
                exclude={"assignment_ref", "explicit_tool_allowlist"},
            ),
            "explicit_tool_allowlist": ("unavailable_tool",),
        }
    )
    with pytest.raises(ValueError, match="Tool scope|assignment reference"):
        factory.context_for_assignment(narrowed_assignment, child, services)

    next_generation = WorkflowProfileAssignment.create(
        **{
            **assignment.model_dump(
                mode="python",
                exclude={"assignment_ref", "execution_generation"},
            ),
            "execution_generation": assignment.execution_generation + 1,
        }
    )
    with pytest.raises(ValueError, match="assignment reference"):
        factory.context_for_assignment(next_generation, child, services)

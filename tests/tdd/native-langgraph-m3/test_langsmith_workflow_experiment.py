from __future__ import annotations

import asyncio
from types import SimpleNamespace
from copy import deepcopy
from uuid import UUID

import pytest
from langsmith.run_helpers import tracing_context
from langsmith.run_trees import RunTree

from assistant_agent.workflows.durable_graph_app import (
    DurableWorkflowGraphApp,
    WorkflowGraphExecutionIdentity,
)
from evals.langsmith_workflow_regression.experiment import (
    DirectWorkflowInvocation,
    REQUIRED_WORKFLOW_FEEDBACK_KEYS,
    audit_native_workflow_tree,
    project_workflow_result,
    run_workflow_example,
    wait_for_workflow_experiment_completeness,
    WorkflowTreeRequirement,
)
from workflow_graph_probe import acceptance, workflow_probe
import evals.langsmith_workflow_regression.cli as workflow_cli


EXAMPLE_ID = UUID("01234567-89ab-cdef-0123-456789abcdef")
TRACE_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _run(run_id: int, *, name: str, parent: int | None, run_type: str = "chain"):
    metadata = {}
    if name == "DurableWorkflowGraph":
        metadata = {
            "execution_engine": "durable_workflow_graph",
            "workflow_id": "sha256:" + "a" * 64,
            "thread_id": "sha256:" + "b" * 64,
            "run_id": "sha256:" + "c" * 64,
        }
    return SimpleNamespace(
        id=UUID(int=run_id),
        parent_run_id=UUID(int=parent) if parent is not None else None,
        name=name,
        run_type=run_type,
        reference_example_id=EXAMPLE_ID if parent is None else None,
        trace_id=TRACE_ID,
        inputs={"value": "input"},
        outputs={"value": "output"},
        extra={"metadata": metadata},
    )


def _detached_run(run_id: int, *, name: str, parent: int | None = None):
    value = _run(run_id, name=name, parent=parent)
    value.reference_example_id = None
    return value


def _native_workflow_runs():
    return [
        _run(1, name="experiment-item-task", parent=None),
        _run(2, name="DurableWorkflowGraph", parent=1),
        _run(3, name="WorkflowPlanningSubgraph", parent=2),
        _run(4, name="AssistantTurnGraph.planner", parent=3),
        _run(5, name="WorkflowWorkerBranch", parent=2),
        _run(6, name="AssistantTurnGraph.worker", parent=5),
        _run(7, name="WorkflowWorkerBranch", parent=2),
        _run(8, name="AssistantTurnGraph.worker", parent=7),
        _run(9, name="join_wave", parent=2),
        _run(10, name="WorkflowVerifierBranch", parent=2),
        _run(11, name="AssistantTurnGraph.verifier", parent=10),
        _run(12, name="decide_verification", parent=2),
    ]


def _verifier_plan():
    return {
        "schema_version": "workflow_plan_v2",
        "nodes": [
            {
                "node_id": node_id,
                "display_title": node_id,
                "objective": f"execute {node_id}",
                "depends_on": dependencies,
                "acceptance_contract": acceptance(f"criterion_{node_id}"),
            }
            for node_id, dependencies in {
                "research_a": [],
                "research_b": [],
                "synthesize": ["research_a", "research_b"],
                "verify": ["synthesize"],
            }.items()
        ],
        "deliverable_bindings": [
            {"deliverable": "report", "producer_node_id": "verify"}
        ],
        "constraint_bindings": [
            {
                "constraint_id": "cite_sources",
                "statement": "cite sources",
                "owner_node_ids": ["synthesize"],
                "verifier_node_id": "verify",
                "severity": "required",
            }
        ],
    }


def test_persisted_tree_audit_requires_one_native_parented_graph_tree() -> None:
    complete = audit_native_workflow_tree(
        _native_workflow_runs(), example_ids=(str(EXAMPLE_ID),)
    )
    assert complete.complete is True

    missing_planner = [
        run for run in _native_workflow_runs() if run.name != "AssistantTurnGraph.planner"
    ]
    assert audit_native_workflow_tree(
        missing_planner, example_ids=(str(EXAMPLE_ID),)
    ).complete is False

    orphan = _native_workflow_runs()
    orphan[5] = _run(6, name="AssistantTurnGraph.worker", parent=None)
    assert audit_native_workflow_tree(
        orphan, example_ids=(str(EXAMPLE_ID),)
    ).complete is False

    no_verifier = [
        run
        for run in _native_workflow_runs()
        if run.name not in {"WorkflowVerifierBranch", "AssistantTurnGraph.verifier"}
    ]
    assert audit_native_workflow_tree(
        no_verifier,
        example_ids=(str(EXAMPLE_ID),),
        requirements={str(EXAMPLE_ID): WorkflowTreeRequirement(require_verifier=False)},
    ).complete is True

    assert audit_native_workflow_tree(
        _native_workflow_runs(),
        example_ids=(str(EXAMPLE_ID),),
        requirements={
            str(EXAMPLE_ID): WorkflowTreeRequirement(
                repair_generations=(("research_a", 1),)
            )
        },
    ).complete is False

    with_llm_tool = [
        *_native_workflow_runs(),
        _run(20, name="llm.chat", parent=4, run_type="llm"),
        _run(21, name="execute_tool", parent=6),
        _run(22, name="governed-tool", parent=21, run_type="tool"),
    ]
    assert audit_native_workflow_tree(
        with_llm_tool, example_ids=(str(EXAMPLE_ID),)
    ).complete is True

    for detached in (
        _detached_run(30, name="WorkflowWorkerBranch"),
        _detached_run(31, name="AssistantTurnGraph.worker", parent=999),
    ):
        assert audit_native_workflow_tree(
            [*_native_workflow_runs(), detached],
            example_ids=(str(EXAMPLE_ID),),
        ).complete is False

    other_trace = _detached_run(32, name="WorkflowWorkerBranch")
    other_trace.trace_id = UUID("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
    assert audit_native_workflow_tree(
        [*_native_workflow_runs(), other_trace],
        example_ids=(str(EXAMPLE_ID),),
    ).complete is False

    assert audit_native_workflow_tree(
        _native_workflow_runs(),
        example_ids=(str(EXAMPLE_ID),),
        requirements={
            str(EXAMPLE_ID): WorkflowTreeRequirement(
                worker_generations=(("research_a", 0), ("missing_worker", 0))
            )
        },
    ).complete is False

    fake_repair = _run(33, name="inner-node", parent=6)
    fake_repair.extra["metadata"] = {
        "workflow_node_id": "research_a",
        "workflow_generation": 1,
        "workflow_branch_run_id": "sha256:" + "d" * 64,
    }
    assert audit_native_workflow_tree(
        [*_native_workflow_runs(), fake_repair],
        example_ids=(str(EXAMPLE_ID),),
        requirements={
            str(EXAMPLE_ID): WorkflowTreeRequirement(
                repair_generations=(("research_a", 1),)
            )
        },
    ).complete is False

    example_two = UUID("11234567-89ab-cdef-0123-456789abcdef")
    trace_two = UUID("cccccccc-dddd-eeee-ffff-aaaaaaaaaaaa")
    second_tree = deepcopy(_native_workflow_runs())
    for run in second_tree:
        run.id = UUID(int=run.id.int + 100)
        run.trace_id = trace_two
        if run.parent_run_id is not None:
            run.parent_run_id = UUID(int=run.parent_run_id.int + 100)
        if run.parent_run_id is None:
            run.reference_example_id = example_two
    assert audit_native_workflow_tree(
        [*_native_workflow_runs(), *second_tree],
        example_ids=(str(EXAMPLE_ID), str(example_two)),
    ).complete is True


def test_completeness_fails_closed_until_all_four_feedback_are_persisted() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def list_runs(self, **_kwargs):
            return iter(_native_workflow_runs())

        def list_feedback(self, **_kwargs):
            self.calls += 1
            keys = (
                REQUIRED_WORKFLOW_FEEDBACK_KEYS[:-1]
                if self.calls == 1
                else REQUIRED_WORKFLOW_FEEDBACK_KEYS
            )
            return iter(
                SimpleNamespace(run_id=UUID(int=1), key=key, score=True)
                for key in keys
            )

    sleeps: list[float] = []
    result = wait_for_workflow_experiment_completeness(
        Client(),
        experiment_id="experiment-id",
        example_ids=(str(EXAMPLE_ID),),
        timeout_seconds=2,
        poll_interval_seconds=1,
        sleep=sleeps.append,
    )

    assert set(result.feedback[str(EXAMPLE_ID)]) == set(
        REQUIRED_WORKFLOW_FEEDBACK_KEYS
    )
    assert sleeps == [1]


def test_direct_target_calls_durable_graph_app_and_projects_only_bounded_facts(
    tmp_path,
) -> None:
    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path,
        {
            "research_a": [],
            "research_b": [],
            "synthesize": ["research_a", "research_b"],
            "verify": ["synthesize"],
        },
        plan_payload=_verifier_plan(),
    )
    app = DurableWorkflowGraphApp(graph)
    identity = WorkflowGraphExecutionIdentity.for_workflow(
        workflow_id="wf-send",
        workflow_thread_id="workflow-thread-send",
        run_id="invoke-send",
        user_id="user-send",
        session_id="session-send",
        agent_id="agent-send",
    )
    async def invoke():
        return await app.arun(initial, identity=identity, context=context), True

    invocation = DirectWorkflowInvocation(invoke=invoke)
    try:
        output = asyncio.run(run_workflow_example(invocation))
    finally:
        artifact_store.close()

    assert output["workflow_id"].startswith("sha256:")
    assert output["workflow_id"] != "wf-send"
    assert output["terminal_status"] == "completed"
    assert output["plan"]["node_ids"] == [
        "research_a",
        "research_b",
        "synthesize",
        "verify",
    ]
    assert output["trajectory"] == [
        {"node_id": "research_a", "generation": 0, "profile": "worker"},
        {"node_id": "research_b", "generation": 0, "profile": "worker"},
        {"node_id": "synthesize", "generation": 0, "profile": "worker"},
        {"node_id": "verify", "generation": 0, "profile": "verifier"},
    ]
    serialized = repr(output)
    assert "execute graph" not in serialized
    assert "completed research_a" not in serialized
    assert "provider" not in serialized.casefold()


def test_actual_compiled_graph_inherits_experiment_parent_through_safe_tracer(
    tmp_path,
) -> None:
    created: list[dict] = []
    updated: list[dict] = []

    class RecordingClient:
        def create_run(self, **kwargs):
            created.append(kwargs)

        def update_run(self, run_id, **kwargs):
            updated.append({"id": run_id, **kwargs})

    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path,
        {
            "research_a": [],
            "research_b": [],
            "synthesize": ["research_a", "research_b"],
            "verify": ["synthesize"],
        },
        plan_payload=_verifier_plan(),
    )
    app = DurableWorkflowGraphApp(graph)
    identity = WorkflowGraphExecutionIdentity.for_workflow(
        workflow_id="wf-send",
        workflow_thread_id="workflow-thread-send",
        run_id="invoke-send",
        user_id="user-send",
        session_id="session-send",
        agent_id="agent-send",
    )
    client = RecordingClient()
    parent = RunTree(
        name="experiment-item-task",
        inputs={"dataset": "must-remain"},
        ls_client=client,
        project_name="project-sentinel",
        reference_example_id=EXAMPLE_ID,
    )

    async def execute():
        with tracing_context(parent=parent, enabled=True, client=client):
            return await app.arun(initial, identity=identity, context=context)

    try:
        result = asyncio.run(execute())
    finally:
        artifact_store.close()

    assert result.status == "completed"
    names = [item["name"] for item in created]
    for required in (
        "DurableWorkflowGraph",
        "WorkflowPlanningSubgraph",
        "AssistantTurnGraph.planner",
        "WorkflowWorkerBranch",
        "AssistantTurnGraph.worker",
        "join_wave",
        "WorkflowVerifierBranch",
        "AssistantTurnGraph.verifier",
    ):
        assert required in names
    graph_run = next(item for item in created if item["name"] == "DurableWorkflowGraph")
    assert graph_run["parent_run_id"] == parent.id
    assert graph_run["inputs"] == {}
    assert all(item.get("inputs") == {} for item in created if item["run_type"] == "chain")
    chain_ids = {item["id"] for item in created if item["run_type"] == "chain"}
    assert all(
        item.get("outputs") in ({}, None)
        for item in updated
        if item["id"] in chain_ids
    )
    chain_persisted = repr(
        (
            [item for item in created if item["run_type"] == "chain"],
            [item for item in updated if item["id"] in chain_ids],
        )
    )
    assert "execute graph" not in chain_persisted
    assert "user-send" not in chain_persisted
    assert "session-send" not in chain_persisted
    metadata = graph_run["extra"]["metadata"]
    assert metadata["run_id"].startswith("sha256:")
    assert metadata["execution_engine"] == "durable_workflow_graph"
    assert metadata["workflow_id"].startswith("sha256:")
    assert metadata["thread_id"].startswith("sha256:")
    branch_metadata = [
        item["extra"]["metadata"]
        for item in created
        if item["name"] == "WorkflowWorkerBranch"
    ]
    assert {item["workflow_node_id"] for item in branch_metadata} >= {
        "research_a",
        "research_b",
        "synthesize",
    }
    assert all(item["workflow_generation"] == 0 for item in branch_metadata)
    assert all(item["workflow_branch_run_id"].startswith("sha256:") for item in branch_metadata)
    assert "invoke-send:research_a:g0" not in repr(branch_metadata)


def test_projector_fails_closed_when_resume_equivalence_has_no_real_evidence():
    with pytest.raises(RuntimeError, match="resume equivalence"):
        project_workflow_result(
            SimpleNamespace(final_state={"workflow_id": "wf"}, status="completed"),
            resume_equivalent=None,
            require_resume_equivalence=True,
        )


def test_cli_inspect_is_local_and_never_creates_langsmith_client(monkeypatch, capsys):
    monkeypatch.setattr(
        workflow_cli,
        "_langsmith_client",
        lambda: (_ for _ in ()).throw(AssertionError("network client")),
    )

    assert workflow_cli.main(["--inspect"]) == 0

    output = __import__("json").loads(capsys.readouterr().out)
    assert output["status"] == "offline_contract_ready"
    assert output["dataset_name"] == "assistant-agent-durable-workflow-regressions"
    assert len(output["case_types"]) == 4


def test_cli_preflight_requires_operator_flags_before_client(monkeypatch):
    monkeypatch.setattr(
        workflow_cli,
        "_langsmith_client",
        lambda: (_ for _ in ()).throw(AssertionError("network client")),
    )

    with pytest.raises(SystemExit):
        workflow_cli.main(["--preflight", "--allow-real-provider"])


def test_operator_gate_rejects_persisted_false_feedback() -> None:
    with pytest.raises(RuntimeError, match="false Feedback"):
        workflow_cli._require_passing_feedback(  # noqa: SLF001
            {
                "example-1": {
                    key: key != REQUIRED_WORKFLOW_FEEDBACK_KEYS[-1]
                    for key in REQUIRED_WORKFLOW_FEEDBACK_KEYS
                }
            }
        )

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from copy import deepcopy
import tempfile
from uuid import UUID

import pytest
from langsmith.run_helpers import tracing_context
from langsmith.run_trees import RunTree
from pydantic import ValidationError

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
from evals.langsmith_workflow_regression.dataset import (
    load_git_workflow_examples,
    sync_workflow_examples,
)
from evals.langsmith_workflow_regression.contracts import WorkflowExampleInput


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


def test_tree_audit_derives_actual_node_identity_from_target_output() -> None:
    runs = _native_workflow_runs()
    root = runs[0]
    root.outputs = {
        "output": {
            "repair_round": 0,
            "plan": {
                "node_ids": ["actual_research_a", "actual_research_b", "actual_verify"],
                "dependencies": {
                    "actual_research_a": [],
                    "actual_research_b": [],
                    "actual_verify": ["actual_research_a", "actual_research_b"],
                },
            },
            "trajectory": [
                {"node_id": "actual_research_a", "generation": 0, "profile": "worker"},
                {"node_id": "actual_research_b", "generation": 0, "profile": "worker"},
                {"node_id": "actual_verify", "generation": 0, "profile": "verifier"},
            ],
        }
    }
    for run, node_id, profile in (
        (runs[4], "actual_research_a", "worker"),
        (runs[6], "actual_research_b", "worker"),
        (runs[9], "actual_verify", "verifier"),
    ):
        run.extra["metadata"] = {
            "workflow_node_id": node_id,
            "workflow_generation": 0,
            "workflow_profile": profile,
            "workflow_branch_run_id": "sha256:" + "d" * 64,
        }
    requirement = WorkflowTreeRequirement(
        require_verifier=True,
        derive_from_root_output=True,
    )

    complete = audit_native_workflow_tree(
        iter(runs),
        example_ids=(str(EXAMPLE_ID),),
        requirements={str(EXAMPLE_ID): requirement},
    )
    assert complete.complete is True

    truncated_page = [run for run in runs if run.id != runs[6].id]
    truncated = audit_native_workflow_tree(
        iter(truncated_page),
        example_ids=(str(EXAMPLE_ID),),
        requirements={str(EXAMPLE_ID): requirement},
    )
    assert truncated.complete is False
    assert any(
        "actual tree trajectory mismatch" in problem
        for problem in truncated.problems[str(EXAMPLE_ID)]
    )

    root.outputs["output"]["trajectory"][0]["generation"] = 1
    runs[4].extra["metadata"]["workflow_generation"] = 1
    resume_only = audit_native_workflow_tree(
        iter(runs),
        example_ids=(str(EXAMPLE_ID),),
        requirements={
            str(EXAMPLE_ID): WorkflowTreeRequirement(
                require_verifier=True,
                derive_from_root_output=True,
                require_repair=True,
            )
        },
    )
    assert resume_only.complete is False
    assert "missing actual repair round" in resume_only.problems[str(EXAMPLE_ID)]

    extra_branch = _run(40, name="WorkflowWorkerBranch", parent=2)
    extra_branch.extra["metadata"] = {
        "workflow_node_id": "unattributed_extra",
        "workflow_generation": 0,
        "workflow_profile": "worker",
        "workflow_branch_run_id": "not-a-digest",
    }
    extra_child = _run(41, name="AssistantTurnGraph.worker", parent=40)
    invalid_extra = audit_native_workflow_tree(
        iter([*runs, extra_branch, extra_child]),
        example_ids=(str(EXAMPLE_ID),),
        requirements={str(EXAMPLE_ID): requirement},
    )
    assert invalid_extra.complete is False
    assert "invalid workflow branch metadata" in invalid_extra.problems[str(EXAMPLE_ID)]

    resume_graph = _run(50, name="DurableWorkflowGraph", parent=1)
    resume_graph.extra["metadata"] = {
        **resume_graph.extra["metadata"],
        "thread_id": "sha256:" + "e" * 64,
        "run_id": "sha256:" + "f" * 64,
    }
    cross_thread = audit_native_workflow_tree(
        iter([*runs, resume_graph]),
        example_ids=(str(EXAMPLE_ID),),
        requirements={str(EXAMPLE_ID): requirement},
    )
    assert cross_thread.complete is False
    assert "inconsistent DurableWorkflowGraph identity" in cross_thread.problems[
        str(EXAMPLE_ID)
    ]


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


def test_projector_reports_safe_graph_failure_before_parsing_missing_plan():
    _graph, _context, initial, _worker, artifact_store = workflow_probe(
        __import__("pathlib").Path("/tmp") / "workflow-projector-failure-probe",
        {"research": []},
        plan_payload=_verifier_plan(),
    )
    failed = dict(initial)
    failed.update(
        {
            "status": "failed",
            "phase": "planning",
            "admitted_plan": None,
            "errors": (
                {
                    "code": "workflow_plan_admission_failed",
                    "message": "provider raw secret must not escape",
                    "node_id": None,
                    "execution_generation": None,
                },
            ),
        }
    )
    try:
        with pytest.raises(RuntimeError) as exc_info:
            project_workflow_result(
                SimpleNamespace(final_state=failed, status="failed"),
                resume_equivalent=False,
                require_resume_equivalence=False,
            )
    finally:
        artifact_store.close()
    message = str(exc_info.value)
    assert message.startswith("workflow_experiment_graph_failed")
    assert "phase=planning" in message
    assert "workflow_plan_admission_failed" in message
    assert "provider raw secret" not in message


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


def test_zero_root_runs_fail_locally_without_unfiltered_feedback_query() -> None:
    class Client:
        feedback_called = False

        def list_runs(self, **_kwargs):
            return iter(())

        def list_feedback(self, **_kwargs):
            self.feedback_called = True
            raise AssertionError("zero root runs must not query all Feedback")

    client = Client()
    with pytest.raises(RuntimeError, match="root_run_count=0"):
        wait_for_workflow_experiment_completeness(
            client,
            experiment_id="empty-experiment",
            example_ids=(str(EXAMPLE_ID),),
            timeout_seconds=0.01,
            poll_interval_seconds=0.01,
            sleep=lambda _seconds: None,
        )
    assert client.feedback_called is False


def test_git_workflow_examples_round_trip_typed_resume_values() -> None:
    examples = load_git_workflow_examples()

    for example in examples:
        round_trip = WorkflowExampleInput.model_validate(
            example.inputs.model_dump(mode="json")
        )
        if round_trip.case_type == "interrupt_resume_equivalence":
            assert round_trip.resume_values_by_field == {
                "research_questions": (
                    "请研究 Durable Workflow 在同一 thread 中断恢复后，"
                    "是否继续原任务并完成最终报告。"
                )
            }
        else:
            assert round_trip.resume_values_by_field == {}

    interrupt = next(
        example.inputs.model_dump(mode="json")
        for example in examples
        if example.inputs.case_type == "interrupt_resume_equivalence"
    )
    interrupt["resume_values_by_field"] = {}
    with pytest.raises(ValidationError):
        WorkflowExampleInput.model_validate(interrupt)

    ordinary = next(
        example.inputs.model_dump(mode="json")
        for example in examples
        if example.inputs.case_type == "parallel_join"
    )
    ordinary["resume_values_by_field"] = {"research_questions": "not allowed"}
    with pytest.raises(ValidationError):
        WorkflowExampleInput.model_validate(ordinary)


def test_dataset_sync_persists_typed_resume_values() -> None:
    class Client:
        def __init__(self) -> None:
            self.inputs = []

        def read_dataset(self, **_kwargs):
            return SimpleNamespace(id=UUID(int=900))

        def list_examples(self, **_kwargs):
            return iter(())

        def create_example(self, *, example_id, inputs, **_kwargs):
            self.inputs.append(inputs)
            return SimpleNamespace(id=example_id)

    client = Client()
    examples = load_git_workflow_examples()

    result = sync_workflow_examples(client, examples, git_commit="deadbeef")

    assert len(result.active_example_ids) == 4
    by_case = {item["case_type"]: item for item in client.inputs}
    assert by_case["interrupt_resume_equivalence"]["resume_values_by_field"]
    assert all(
        item["resume_values_by_field"] == {}
        for case_type, item in by_case.items()
        if case_type != "interrupt_resume_equivalence"
    )


def test_interrupt_resume_factory_uses_only_git_owned_typed_values() -> None:
    captured = {}

    class Host:
        async def arun_submission(self, **kwargs):
            captured["resume_values_factory"] = kwargs.get("resume_values_factory")
            return SimpleNamespace(status="interrupted", final_state={})

    composition = workflow_cli.ProductionWorkflowExperimentComposition(
        run_name="controlled-resume-probe",
        model="model-probe",
        temporary_directory=tempfile.TemporaryDirectory(),
        owner=SimpleNamespace(),
        workflow_host=Host(),
        runtime_host=SimpleNamespace(),
    )
    example = next(
        item
        for item in load_git_workflow_examples()
        if item.inputs.case_type == "interrupt_resume_equivalence"
    )
    invocation = composition.invocation_factory(
        example.inputs.model_dump(mode="json"),
        example_id=example.id,
        reference_output=example.outputs,
    )

    asyncio.run(invocation.invoke())

    assert callable(captured["resume_values_factory"])
    interrupts = (
        SimpleNamespace(
            action_ref="workflow:test:node:a:generation:0",
            required_fields=("research_questions",),
        ),
    )
    first = captured["resume_values_factory"](interrupts)
    second = captured["resume_values_factory"](interrupts)
    assert first == second
    values = first[interrupts[0].action_ref]
    assert values == example.inputs.resume_values_by_field

    unknown = (
        SimpleNamespace(
            action_ref="workflow:test:node:a:generation:1",
            required_fields=("tool_access",),
        ),
    )
    with pytest.raises(RuntimeError, match="workflow_eval_resume_field_missing"):
        captured["resume_values_factory"](unknown)

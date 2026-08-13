from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

import asyncio

import pytest
from pydantic import ValidationError

from evals.release_review.langsmith_backend import (
    GIT_EXAMPLE_OWNER,
    LangSmithDatasetSyncResult,
    REQUIRED_RELEASE_FEEDBACK_KEYS,
    RELEASE_REVIEW_DATASET,
    ReleaseExampleBinding,
    ReleaseLangSmithCompletenessResult,
    sync_langsmith_examples,
    wait_for_langsmith_runs,
)
from evals.release_review.contracts import ReleaseScenario
from evals.release_review.loader import scenario_hash
from evals.release_review.experiment import (
    ReleaseExperimentSettings,
    _run_release_item,
    run_release_experiment,
)
from evals.release_review.report import ReleaseItemAssessment
from evals.release_review.service import ReleaseReviewRequest, ReleaseReviewService
from evals.langsmith_runtime_regression.experiment import (
    LangSmithCompletenessResult,
    LangSmithRuntimeRegressionResult,
    audit_native_graph_tree,
    runtime_regression_equivalence_evidence,
)
from evals.langsmith_feedback import normalize_boolean_feedback_score
from evals.langsmith_workflow_regression.experiment import (
    WorkflowCompletenessResult,
    WorkflowExperimentResult,
    workflow_regression_equivalence_evidence,
)
from evals.release_review.report import (
    LangSmithEquivalenceReport,
    LangSmithTargetEvidence,
)
from evals.release_review.staging import CleanupResult


EXAMPLE_ID = UUID("01234567-89ab-cdef-0123-456789abcdef")
TRACE_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
STABLE_EVAL_SCRIPTS = (
    "run_release_review.py",
    "run_langsmith_runtime_regressions.py",
    "run_langsmith_workflow_regressions.py",
)


@pytest.fixture
def foreign_assistant_agent(tmp_path: Path) -> Path:
    source = tmp_path / "foreign-checkout" / "src"
    package = source / "assistant_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('CHECKOUT = "foreign"\n', encoding="utf-8")
    return source


@pytest.mark.parametrize("script_name", STABLE_EVAL_SCRIPTS)
def test_stable_eval_script_prioritizes_its_checkout_source(
    script_name: str,
    foreign_assistant_agent: Path,
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(foreign_assistant_agent)
    script = PROJECT_ROOT / "scripts" / script_name
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy, sys; "
                "runpy.run_path(sys.argv[1], run_name='bootstrap_probe'); "
                "import assistant_agent; print(assistant_agent.__file__)"
            ),
            str(script),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (
        Path(result.stdout.strip()).resolve()
        == (PROJECT_ROOT / "src" / "assistant_agent" / "__init__.py").resolve()
    )


@pytest.mark.parametrize("script_name", STABLE_EVAL_SCRIPTS)
def test_stable_eval_script_rejects_preloaded_foreign_checkout(
    script_name: str,
    foreign_assistant_agent: Path,
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(foreign_assistant_agent)
    script = PROJECT_ROOT / "scripts" / script_name
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import assistant_agent, runpy, sys; "
                "runpy.run_path(sys.argv[1], run_name='bootstrap_probe')"
            ),
            str(script),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "current checkout" in result.stderr


def _run(
    run_id: int,
    *,
    name: str,
    parent: int | None,
    run_type: str = "chain",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID(int=run_id),
        parent_run_id=UUID(int=parent) if parent is not None else None,
        name=name,
        run_type=run_type,
        reference_example_id=EXAMPLE_ID if parent is None else None,
        trace_id=TRACE_ID,
        inputs={"request": "probe"},
        outputs={"content": "done"},
    )


def _release_runs() -> tuple[SimpleNamespace, ...]:
    return (
        _run(1, name="experiment-item-task", parent=None),
        _run(2, name="AssistantTurnGraph", parent=1),
        _run(3, name="assistant", parent=2),
        _run(4, name="llm.chat", parent=3, run_type="llm"),
        _run(5, name="execute_tool", parent=2),
        _run(6, name="probe-tool", parent=5, run_type="tool"),
        _run(7, name="compose_response", parent=2),
    )


def _scenario() -> ReleaseScenario:
    return ReleaseScenario.model_validate(
        {
            "id": "decision_probe",
            "phase": "decision",
            "capability": "probe",
            "risk": "high",
            "request": "run probe",
            "tool_contract": {
                "required": ["probe_tool"],
                "allowed": [],
                "forbidden": [],
            },
            "fixtures": {
                "probe_tool": [{"success": True, "data": {"source": "fixture"}}]
            },
            "state_assertions": [{"path": "status", "equals": "completed"}],
        }
    )


def _evidence(
    target: str,
    *,
    feedback_complete: bool = True,
) -> LangSmithTargetEvidence:
    required = ("quality", "grounding")
    feedback = {"example-1": {"quality": True, "grounding": True}}
    if not feedback_complete:
        feedback["example-1"].pop("grounding")
    return LangSmithTargetEvidence(
        target=target,
        dataset_id=f"{target}-dataset",
        project_id=f"{target}-project",
        experiment_id=f"{target}-experiment",
        active_example_ids=("example-1",),
        root_run_ids=("run-1",),
        required_feedback=required,
        feedback=feedback,
        native_tree_complete=True,
    )


def test_equivalence_requires_all_native_trees_and_feedback() -> None:
    report = LangSmithEquivalenceReport(
        release_review=_evidence("release_review"),
        runtime_regression=_evidence("runtime_regression"),
        workflow_regression=_evidence(
            "workflow_regression",
            feedback_complete=False,
        ),
    )

    assert report.approved is False
    assert report.langsmith_equivalence == "blocked"
    assert report.blockers == ("workflow_regression_feedback_incomplete",)


def test_equivalence_approves_only_complete_remote_evidence() -> None:
    report = LangSmithEquivalenceReport(
        release_review=_evidence("release_review"),
        runtime_regression=_evidence("runtime_regression"),
        workflow_regression=_evidence("workflow_regression"),
    )

    assert report.approved is True
    assert report.langsmith_equivalence == "approved"
    assert report.blockers == ()
    assert all(
        item.complete
        for item in (
            report.release_review,
            report.runtime_regression,
            report.workflow_regression,
        )
    )


def test_equivalence_rejects_mislabeled_target_or_non_boolean_feedback() -> None:
    with pytest.raises(ValidationError):
        LangSmithEquivalenceReport(
            release_review=_evidence("runtime_regression"),
            runtime_regression=_evidence("runtime_regression"),
            workflow_regression=_evidence("workflow_regression"),
        )

    payload = _evidence("release_review").model_dump()
    payload["feedback"] = {"example-1": {"quality": 1, "grounding": True}}
    with pytest.raises(ValidationError):
        LangSmithTargetEvidence.model_validate(payload)


def test_release_completeness_uses_all_persisted_pages_and_feedback() -> None:
    class Client:
        def __init__(self) -> None:
            self.feedback_calls = 0
            self.run_queries: list[dict[str, object]] = []

        def list_runs(self, **kwargs):
            self.run_queries.append(kwargs)

            def paginated():
                yield from _release_runs()[:3]
                yield from _release_runs()[3:]

            return paginated()

        def list_feedback(self, **_kwargs):
            self.feedback_calls += 1
            keys = (
                REQUIRED_RELEASE_FEEDBACK_KEYS[:-1]
                if self.feedback_calls == 1
                else REQUIRED_RELEASE_FEEDBACK_KEYS
            )
            return iter(
                SimpleNamespace(run_id=UUID(int=1), key=key, score=True) for key in keys
            )

    client = Client()
    sleeps: list[float] = []
    result = wait_for_langsmith_runs(
        client,
        experiment_id="experiment-id",
        example_ids=(str(EXAMPLE_ID),),
        timeout_seconds=2,
        poll_interval_seconds=1,
        sleep=sleeps.append,
    )

    assert result.native_tree_complete is True
    assert result.root_run_ids == (str(UUID(int=1)),)
    assert set(result.feedback[str(EXAMPLE_ID)]) == set(REQUIRED_RELEASE_FEEDBACK_KEYS)
    assert sleeps == [1]
    assert len(client.run_queries) == 2
    assert all(query["project_id"] == "experiment-id" for query in client.run_queries)
    assert all("limit" not in query for query in client.run_queries)
    assert all("start_time" not in query for query in client.run_queries)


def test_release_completeness_rejects_in_memory_or_stream_only_success() -> None:
    class Client:
        def list_runs(self, **_kwargs):
            return iter(())

        def list_feedback(self, **_kwargs):
            return iter(
                SimpleNamespace(run_id=UUID(int=1), key=key, score=True)
                for key in REQUIRED_RELEASE_FEEDBACK_KEYS
            )

    with pytest.raises(RuntimeError, match="incomplete"):
        wait_for_langsmith_runs(
            Client(),
            experiment_id="experiment-id",
            example_ids=(str(EXAMPLE_ID),),
            timeout_seconds=0.01,
            poll_interval_seconds=0.01,
            sleep=lambda _seconds: None,
            clock=iter((0.0, 0.0, 0.02)).__next__,
        )


def test_release_completeness_normalizes_persisted_zero_one_scores() -> None:
    class Client:
        def list_runs(self, **_kwargs):
            return iter(_release_runs())

        def list_feedback(self, **_kwargs):
            return iter(
                SimpleNamespace(
                    run_id=UUID(int=1),
                    key=key,
                    score=score,
                )
                for key, score in zip(
                    REQUIRED_RELEASE_FEEDBACK_KEYS,
                    (1.0, 0.0, 1.0),
                    strict=True,
                )
            )

    result = wait_for_langsmith_runs(
        Client(),
        experiment_id="experiment-id",
        example_ids=(str(EXAMPLE_ID),),
        timeout_seconds=1,
        poll_interval_seconds=1,
        sleep=lambda _seconds: None,
    )

    assert result.feedback[str(EXAMPLE_ID)] == {
        REQUIRED_RELEASE_FEEDBACK_KEYS[0]: True,
        REQUIRED_RELEASE_FEEDBACK_KEYS[1]: False,
        REQUIRED_RELEASE_FEEDBACK_KEYS[2]: True,
    }


@pytest.mark.parametrize(
    "score",
    [
        Decimal("0"),
        Decimal("1"),
        Fraction(0, 1),
        Fraction(1, 1),
        complex(1, 0),
        Decimal("NaN"),
    ],
)
def test_feedback_normalizer_rejects_non_sdk_numeric_types(score) -> None:
    with pytest.raises(ValueError, match="not Boolean"):
        normalize_boolean_feedback_score(score)


@pytest.mark.parametrize("score", [0.5, "1", float("nan")])
def test_release_completeness_rejects_non_boolean_numeric_scores(score) -> None:
    class Client:
        def list_runs(self, **_kwargs):
            return iter(_release_runs())

        def list_feedback(self, **_kwargs):
            return iter(
                SimpleNamespace(run_id=UUID(int=1), key=key, score=score)
                for key in REQUIRED_RELEASE_FEEDBACK_KEYS
            )

    with pytest.raises(RuntimeError, match="invalid feedback"):
        wait_for_langsmith_runs(
            Client(),
            experiment_id="experiment-id",
            example_ids=(str(EXAMPLE_ID),),
            timeout_seconds=0.01,
            poll_interval_seconds=0.01,
            sleep=lambda _seconds: None,
            clock=iter((0.0, 0.0, 0.02)).__next__,
        )


def test_shared_assistant_tree_audit_rejects_detached_native_graph_run() -> None:
    detached = _run(90, name="AssistantTurnGraph", parent=999)
    detached.reference_example_id = None

    result = audit_native_graph_tree(
        (*_release_runs(), detached),
        example_ids=(str(EXAMPLE_ID),),
    )

    assert result.complete is False
    assert "detached native graph run detected" in result.problems[str(EXAMPLE_ID)]


def test_langsmith_sync_keeps_git_scenarios_authoritative_and_archives_stale() -> None:
    stale_id = UUID("10000000-0000-0000-0000-000000000001")

    class Client:
        def __init__(self) -> None:
            self.dataset = SimpleNamespace(
                id=UUID("20000000-0000-0000-0000-000000000001"),
                name=RELEASE_REVIEW_DATASET,
            )
            self.examples = [
                SimpleNamespace(
                    id=stale_id,
                    inputs={"scenario_id": "removed", "request": "old"},
                    outputs={},
                    metadata={
                        "owner": GIT_EXAMPLE_OWNER,
                        "scenario_id": "removed",
                        "repetition": 1,
                        "active": True,
                    },
                ),
                SimpleNamespace(
                    id=UUID("30000000-0000-0000-0000-000000000001"),
                    inputs={"request": "operator-owned"},
                    outputs={},
                    metadata={"owner": "operator", "active": True},
                ),
            ]
            self.created: list[dict[str, object]] = []
            self.updated: list[tuple[str, dict[str, object]]] = []

        def read_dataset(self, *, dataset_name):
            assert dataset_name == RELEASE_REVIEW_DATASET
            return self.dataset

        def list_examples(self, *, dataset_id):
            assert str(dataset_id) == str(self.dataset.id)
            return iter(self.examples)

        def create_example(self, **kwargs):
            self.created.append(kwargs)
            return SimpleNamespace(id=kwargs["example_id"])

        def update_example(self, example_id, **kwargs):
            self.updated.append((str(example_id), kwargs))

    client = Client()
    scenario = _scenario()
    result = sync_langsmith_examples(client, (scenario,), "git-sentinel")

    assert result.dataset_name == RELEASE_REVIEW_DATASET
    assert len(result.active_example_ids) == 1
    assert result.archived_example_ids == (str(stale_id),)
    assert client.created[0]["inputs"] == {
        "scenario_id": scenario.id,
        "request": scenario.request,
    }
    assert client.created[0]["outputs"]["tool_contract"]["required"] == ["probe_tool"]
    assert client.created[0]["metadata"]["scenario_hash"] == scenario_hash(scenario)
    assert client.updated == [
        (
            str(stale_id),
            {
                "metadata": {
                    "owner": GIT_EXAMPLE_OWNER,
                    "scenario_id": "removed",
                    "repetition": 1,
                    "active": False,
                }
            },
        )
    ]


def test_release_experiment_uses_langsmith_target_and_actual_runtime() -> None:
    scenario = _scenario()
    example = SimpleNamespace(
        id=EXAMPLE_ID,
        inputs={"scenario_id": scenario.id, "request": scenario.request},
        outputs={},
        metadata={
            "owner": GIT_EXAMPLE_OWNER,
            "active": True,
            "scenario_id": scenario.id,
            "phase": scenario.phase,
            "risk": scenario.risk,
            "scenario_hash": scenario_hash(scenario),
            "repetition": 1,
        },
    )
    current_run = SimpleNamespace(
        id=UUID(int=1),
        trace_id=TRACE_ID,
        reference_example_id=EXAMPLE_ID,
    )

    class State:
        run_id = "runtime-run"
        status = "completed"
        tool_calls = ()
        tool_results = ()
        errors = ()
        response = SimpleNamespace(
            message="done",
            model_dump=lambda **_kwargs: {"message": "done"},
        )

    class Runtime:
        def __init__(self) -> None:
            self.trace_store = SimpleNamespace(
                list_by_run=lambda _run_id: [{"canonical_event": "response.delivered"}]
            )
            self.closed = False
            self.requests = []

        async def arun_state(self, request):
            self.requests.append(request)
            return State()

        def close(self):
            self.closed = True
            return True

    runtime = Runtime()

    class NativeResult:
        experiment_id = UUID(int=30)
        experiment_name = "release-experiment"
        url = "https://smith.invalid/experiment"

        def __aiter__(self):
            async def rows():
                yield SimpleNamespace(
                    example=example,
                    run=SimpleNamespace(id=UUID(int=1)),
                )

            return rows()

        async def get_dataset_id(self):
            return UUID(int=20)

    class Client:
        def __init__(self) -> None:
            self.aevaluate_kwargs = None

        def read_dataset(self, *, dataset_name):
            assert dataset_name == RELEASE_REVIEW_DATASET
            return SimpleNamespace(id=UUID(int=20))

        def list_examples(self, *, dataset_id):
            assert str(dataset_id) == str(UUID(int=20))
            return iter((example,))

        async def aevaluate(self, target, **kwargs):
            self.aevaluate_kwargs = kwargs
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(
                    "evals.release_review.experiment._current_run_tree",
                    lambda: current_run,
                )
                output = await target(example.inputs)
            assert output["scenario_id"] == scenario.id
            assert output["scenario_hash"] == scenario_hash(scenario)
            assert output["evidence"]["final_state"]["status"] == "completed"
            return NativeResult()

        def create_project(self, name, **kwargs):
            assert name.startswith("release-run-")
            assert kwargs["reference_dataset_id"] == UUID(int=20)
            return SimpleNamespace(id=UUID(int=30), name=name)

    client = Client()
    result = asyncio.run(
        run_release_experiment(
            client,
            (scenario,),
            ReleaseExperimentSettings(
                release_id="release-1",
                model="model",
                git_commit="git",
                catalog_generation="catalog",
                evaluator_version="evaluator",
                runtime_factory=lambda _scenario, backend, _metadata: (
                    runtime
                    if backend is not None
                    else pytest.fail("Decision backend is required")
                ),
                run_name="release-run",
            ),
        )
    )

    assert result.experiment_id == str(UUID(int=30))
    assert result.dataset_id == str(UUID(int=20))
    assert result.example_ids == (str(EXAMPLE_ID),)
    assert result.run_ids == (str(UUID(int=1)),)
    assert client.aevaluate_kwargs["data"] == [example]
    assert client.aevaluate_kwargs["evaluators"]
    assert runtime.requests[0].text == scenario.request
    assert runtime.closed is True


def test_staging_cleanup_and_slot_release_survive_runtime_close_failure() -> None:
    scenario = ReleaseScenario.model_validate(
        {
            "id": "staging_probe",
            "phase": "staging",
            "capability": "probe",
            "risk": "high",
            "request": "run probe",
            "tool_contract": {
                "required": ["probe_tool"],
                "allowed": [],
                "forbidden": [],
            },
            "state_assertions": [{"path": "status", "equals": "completed"}],
            "staging": {
                "resource_profile": "test_calendar",
                "cleanup": "required",
            },
        }
    )
    cleanup_result = CleanupResult(status="succeeded")

    class Lease:
        runtime_metadata = {"release_review": {"namespace": "staging-user"}}

        def __init__(self) -> None:
            self.cleaned = False

        def cleanup(self):
            self.cleaned = True
            return cleanup_result

    lease = Lease()

    class Resources:
        def prepare(self, release_id, selected_scenario):
            assert release_id == "release-1"
            assert selected_scenario is scenario
            return lease

    class State:
        run_id = "runtime-run"
        status = "completed"
        tool_calls = ()
        tool_results = ()
        errors = ()
        response = SimpleNamespace(
            message="done",
            model_dump=lambda **_kwargs: {"message": "done"},
        )

    class Runtime:
        trace_store = SimpleNamespace(list_by_run=lambda _run_id: ())

        async def arun_state(self, _request):
            return State()

        def close(self):
            return False

    settings = ReleaseExperimentSettings(
        release_id="release-1",
        model="model",
        git_commit="git",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        runtime_factory=lambda _scenario, _backend, _metadata: Runtime(),
        staging_resources=Resources(),
    )
    cleanup_results = {}

    async def exercise() -> None:
        slots = asyncio.Semaphore(1)
        with pytest.raises(RuntimeError, match="failed to close"):
            await _run_release_item(
                scenario,
                repetition=1,
                settings=settings,
                progress=None,
                staging_slots=slots,
                cleanup_results=cleanup_results,
            )
        assert slots.locked() is False

    asyncio.run(exercise())

    assert lease.cleaned is True
    assert cleanup_results == {"staging_probe:r1": cleanup_result}


def test_release_service_waits_for_remote_langsmith_evidence_before_report(
    tmp_path,
) -> None:
    scenario = _scenario()
    calls: list[str] = []
    binding = ReleaseExampleBinding(
        example_id=str(EXAMPLE_ID),
        scenario_id=scenario.id,
        repetition=1,
        scenario_hash=scenario_hash(scenario),
    )
    sync_result = LangSmithDatasetSyncResult(
        dataset_name=RELEASE_REVIEW_DATASET,
        dataset_id="dataset-id",
        active_example_ids=(str(EXAMPLE_ID),),
        archived_example_ids=(),
        bindings=(binding,),
    )
    experiment = SimpleNamespace(
        experiment_id="experiment-id",
        experiment_name="experiment-name",
        experiment_url="https://smith.invalid/experiment",
        dataset_id="dataset-id",
        example_ids=(str(EXAMPLE_ID),),
        run_ids=("sdk-row-run",),
        cleanup_results={},
    )
    completeness = ReleaseLangSmithCompletenessResult(
        example_ids=(str(EXAMPLE_ID),),
        root_run_ids=("persisted-root-run",),
        feedback={
            str(EXAMPLE_ID): {key: True for key in REQUIRED_RELEASE_FEEDBACK_KEYS}
        },
        native_tree_complete=True,
    )
    assessment = ReleaseItemAssessment(
        scenario_id=scenario.id,
        repetition=1,
        phase=scenario.phase,
        risk=scenario.risk,
        scenario_hash=scenario_hash(scenario),
        run_id="persisted-root-run",
        scores={key: True for key in REQUIRED_RELEASE_FEEDBACK_KEYS},
    )

    async def run_experiment(_client, selected, _settings, **_kwargs):
        calls.append("experiment")
        assert selected == (scenario,)
        return experiment

    service = ReleaseReviewService(
        client=SimpleNamespace(flush=lambda: calls.append("flush")),
        scenario_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        load_scenarios_fn=lambda _root: (scenario,),
        settings_factory=lambda _request, _selected: SimpleNamespace(
            model="model",
            git_commit="git",
            catalog_generation="catalog",
            evaluator_version="evaluator",
        ),
        sync_examples_fn=lambda _client, all_scenarios, _commit: (
            calls.append("sync") or sync_result
        ),
        experiment_runner=run_experiment,
        wait_for_runs_fn=lambda _client, **kwargs: calls.append("wait") or completeness,
        feedback_auditor=lambda complete, bindings, selected, cleanup: (
            calls.append("audit") or (assessment,)
        ),
    )

    report = service.run(ReleaseReviewRequest(release_id="release-1"))

    assert calls == ["sync", "experiment", "flush", "wait", "audit"]
    assert report.langsmith_evidence is not None
    assert report.langsmith_evidence.complete is True
    assert report.langsmith_evidence.dataset_id == "dataset-id"
    assert report.langsmith_evidence.root_run_ids == ("persisted-root-run",)
    assert report.experiment_run_id == "experiment-id"


def test_runtime_and_workflow_results_project_to_the_same_gate_contract() -> None:
    runtime_result = LangSmithRuntimeRegressionResult(
        native_result=object(),
        experiment_id="runtime-project",
        experiment_name="runtime-experiment",
        experiment_url=None,
        dataset_id="runtime-dataset",
        example_ids=("runtime-example",),
        run_ids=("sdk-runtime-row",),
    )
    runtime_complete = LangSmithCompletenessResult(
        run_ids=("runtime-root",),
        feedback={
            "runtime-example": {
                "assistant_agent.quality.response_quality.experiment": True,
                "assistant_agent.quality.grounding.experiment": True,
                "assistant_agent.quality.regression_improvement.experiment": True,
            }
        },
    )
    workflow_result = WorkflowExperimentResult(
        native_result=object(),
        experiment_id="workflow-project",
        experiment_name="workflow-experiment",
        experiment_url=None,
        dataset_id="workflow-dataset",
        example_ids=("workflow-example",),
        run_ids=("sdk-workflow-row",),
        tree_requirements={},
    )
    workflow_complete = WorkflowCompletenessResult(
        run_ids=("workflow-root",),
        feedback={
            "workflow-example": {
                "assistant_agent.workflow.plan_admission": True,
                "assistant_agent.workflow.dag_trajectory": True,
                "assistant_agent.workflow.constraint_artifact_quality": True,
                "assistant_agent.workflow.repair_resume": True,
            }
        },
    )

    runtime_evidence = runtime_regression_equivalence_evidence(
        runtime_result,
        runtime_complete,
    )
    workflow_evidence = workflow_regression_equivalence_evidence(
        workflow_result,
        workflow_complete,
    )

    assert runtime_evidence.target == "runtime_regression"
    assert workflow_evidence.target == "workflow_regression"
    assert runtime_evidence.root_run_ids == ("runtime-root",)
    assert workflow_evidence.root_run_ids == ("workflow-root",)
    assert runtime_evidence.complete is True
    assert workflow_evidence.complete is True

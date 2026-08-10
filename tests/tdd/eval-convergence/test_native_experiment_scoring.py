from types import SimpleNamespace

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.requests import UserRequest
from evals.agent.contracts import (
    EnvironmentValidation,
    RunEvidence,
    TaskExecution,
    TaskSpec,
)
from evals.agent import grading
from evals.agent.grading import rule_assertion
from evals.agent.langfuse_backend import (
    run_tasks,
    verify_persisted_dimension_scores,
)


SCORE_PREFIX = "assistant_agent.quality."


def _native_task() -> TaskSpec:
    return TaskSpec(
        id="native_evaluator_task",
        description="native evaluator task",
        capability="native evaluator",
        request=UserRequest(
            user_id="eval-user",
            session_id="eval-session",
            text="evaluate this task",
        ),
        environment="evals.agent.environment:Environment",
    )


def test_native_evaluator_task_does_not_require_legacy_grader_entrypoint() -> None:
    task = _native_task()

    assert task.grader is None


class ValidEnvironment:
    def __init__(self, **_: object) -> None:
        pass

    def validate(self) -> EnvironmentValidation:
        return EnvironmentValidation(
            passed=True,
            reason="valid",
            checks={"valid": rule_assertion(True, "valid", label="valid")},
        )

    def tool_outcome_expectations(self, available_tools=None):
        assert available_tools == []
        return []

    def execute(self, *, task, request, trace_id, parent_span_id):
        del request, parent_span_id
        return TaskExecution(
            evidence=RunEvidence(
                task_id=task.id,
                run_id="run-eval",
                trace_id=trace_id,
                terminal_status="completed",
                response={"message": "请告诉我需要查询哪个城市。"},
            ),
            trace_events=[],
        )


def test_grade_task_conformance_uses_only_environment_rules(monkeypatch) -> None:
    task = _native_task()
    monkeypatch.setattr(
        "evals.agent.loader.load_entrypoint",
        lambda _: ValidEnvironment,
    )
    monkeypatch.setattr(
        "evals.agent.loader.load_case_source",
        lambda _: SimpleNamespace(level="task"),
    )

    result = grading.grade_task_conformance(
        task=task,
        evidence=RunEvidence(
            task_id=task.id,
            run_id="run-rule",
            trace_id="1" * 32,
            terminal_status="completed",
        ),
    )

    assert result.passed is True
    assert set(result.assertions) == {"outcome_matches_environment"}
    assert all(
        assertion.evaluation_method == "rule"
        for assertion in result.assertions.values()
    )


def test_run_tasks_emits_only_rule_owned_task_conformance(monkeypatch) -> None:
    task = _native_task()
    item = SimpleNamespace(
        input={
            "task_id": task.id,
            "request": task.request.model_dump(mode="json"),
        },
        metadata={"task_id": task.id},
        status="ACTIVE",
    )

    class Dataset:
        def __init__(self) -> None:
            self.items = [item]

        def run_experiment(self, *, task, evaluators, **kwargs):
            del kwargs
            output = task(item=item)
            evaluations = evaluators[0](
                output=output,
                metadata=item.metadata,
            )
            return SimpleNamespace(
                item_results=[
                    SimpleNamespace(
                        trace_id="2" * 32,
                        evaluations=evaluations,
                    )
                ],
                run_name="run-native",
            )

    class Client:
        def get_dataset(self, name):
            del name
            return Dataset()

        def get_current_trace_id(self):
            return "2" * 32

        def get_current_observation_id(self):
            return "parent-observation"

    monkeypatch.setattr(
        "evals.agent.langfuse_backend.load_entrypoint",
        lambda _: ValidEnvironment,
    )
    monkeypatch.setattr(
        "evals.agent.langfuse_backend.load_task",
        lambda _: task,
    )
    monkeypatch.setattr(
        "evals.agent.loader.load_entrypoint",
        lambda _: ValidEnvironment,
    )
    monkeypatch.setattr(
        "evals.agent.loader.load_case_source",
        lambda _: SimpleNamespace(level="task"),
    )

    result = run_tasks(
        Client(),
        [task],
        config=ProviderConfig(provider_mode="mock"),
    )

    assert [item.name for item in result.item_results[0].evaluations] == [
        f"{SCORE_PREFIX}task_conformance"
    ]


def test_persisted_score_audit_is_observation_scoped_and_returns_values() -> None:
    queries: list[dict[str, object]] = []

    class Observations:
        def get_many(self, **query):
            return SimpleNamespace(data=[SimpleNamespace(id="task-observation")])

    class Scores:
        def get_many_v3(self, **query):
            queries.append(query)
            values = {
                f"{SCORE_PREFIX}task_conformance": True,
                f"{SCORE_PREFIX}grounding": False,
                f"{SCORE_PREFIX}response_quality": True,
            }
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        name=name,
                        value=value,
                        data_type="BOOLEAN",
                        subject=SimpleNamespace(
                            kind="observation",
                            trace_id="3" * 32,
                            id="task-observation",
                        ),
                    )
                    for name, value in values.items()
                ]
            )

    client = SimpleNamespace(
        flush=lambda: None,
        api=SimpleNamespace(
            observations=Observations(),
            scores_v3=Scores(),
        ),
    )
    result = SimpleNamespace(
        item_results=[SimpleNamespace(trace_id="3" * 32)],
    )

    scores = verify_persisted_dimension_scores(
        client,
        result,
        attempts=1,
        retry_delay_seconds=0,
    )

    assert scores == [
        {
            "task_conformance": True,
            "grounding": False,
            "response_quality": True,
        }
    ]
    assert queries[0]["observation_id"] == "task-observation"

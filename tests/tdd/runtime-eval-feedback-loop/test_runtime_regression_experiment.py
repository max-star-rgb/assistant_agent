from __future__ import annotations

from types import SimpleNamespace

from assistant_agent.runtime.requests import AgentResponse
from assistant_agent.runtime.state import AgentState
from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET
from evals.runtime_regression.experiment import (
    RuntimeRegressionExperimentSettings,
    run_runtime_regression_experiment,
    wait_for_runtime_regression_scores,
    wait_for_runtime_regression_trace_completeness,
)


class _TraceStore:
    def list_by_run(self, run_id):
        assert run_id == "run-regression"
        return [{"canonical_event": "response.delivered"}]


class _Runtime:
    def __init__(self) -> None:
        self.trace_store = _TraceStore()
        self.requests = []
        self.closed = False

    def run_state(self, request):
        self.requests.append(request)
        state = AgentState.from_request(
            request,
            run_id="run-regression",
            trace_id="trace-regression-rerun",
        )
        state.status = "completed"
        state.response = AgentResponse(message="重跑后的回答")
        return state

    def close(self):
        self.closed = True
        return True


class _Dataset:
    def __init__(self, client) -> None:
        self.client = client
        self.items = [
            SimpleNamespace(
                id="runtime-item-1",
                status="ACTIVE",
                input={
                    "role": "user",
                    "content": "请重跑真实失败案例",
                    "chars": 10,
                    "truncated": False,
                },
                expected_output={
                    "role": "assistant",
                    "content": "原始失败回答",
                    "chars": len("原始失败回答"),
                    "truncated": False,
                    "terminal_status": "completed",
                },
                metadata={"source": "langfuse-ui"},
            )
        ]
        self.call = None
        self.task_output = None

    def run_experiment(self, **kwargs):
        self.call = kwargs
        with self.client.start_as_current_observation(
            name="experiment-item-task",
            as_type="span",
        ) as task_observation:
            self.task_output = kwargs["task"](item=self.items[0])
            task_observation.update(output=self.task_output)
        return SimpleNamespace(
            run_name=kwargs["run_name"],
            dataset_run_id="dataset-run-1",
            dataset_run_url="http://langfuse/run/1",
        )


class _Client:
    def __init__(self) -> None:
        self.observations = []
        self.observation_stack = []
        self.dataset = _Dataset(self)

    def get_dataset(self, name):
        assert name == RUNTIME_REGRESSION_DATASET
        return self.dataset

    def update_current_span(self, *, input=None, **kwargs):
        if not self.observation_stack:
            raise AssertionError("no current Langfuse observation")
        self.observation_stack[-1].input = input

    def start_as_current_observation(self, **kwargs):
        observation = SimpleNamespace(
            kwargs=kwargs,
            input=kwargs.get("input"),
            output=None,
        )

        def update(**values):
            for name in ("input", "output"):
                if name in values:
                    setattr(observation, name, values[name])

        observation.update = update

        class Context:
            def __enter__(self):
                self_observations.append(observation)
                self_observation_stack.append(observation)
                return observation

            def __exit__(self, exc_type, exc, traceback):
                assert self_observation_stack.pop() is observation
                return False

        self_observations = self.observations
        self_observation_stack = self.observation_stack
        return Context()


def test_runtime_regression_experiment_replays_active_item_through_runtime() -> None:
    client = _Client()
    runtimes = []

    def runtime_factory():
        runtime = _Runtime()
        runtimes.append(runtime)
        return runtime

    result = run_runtime_regression_experiment(
        client,
        RuntimeRegressionExperimentSettings(
            model="production-model",
            runtime_factory=runtime_factory,
            run_name="runtime-regression-first-run",
            max_concurrency=1,
        ),
    )

    assert result.run_name == "runtime-regression-first-run"
    assert result.dataset_run_id == "dataset-run-1"
    assert result.dataset_item_ids == ("runtime-item-1",)
    assert len(runtimes) == 1
    request = runtimes[0].requests[0]
    assert request.user_id == "runtime-regression"
    assert request.session_id == "runtime-regression-runtime-item-1"
    assert request.text == "请重跑真实失败案例"
    assert request.metadata == {
        "runtime_regression": {"dataset_item_id": "runtime-item-1"}
    }
    assert len(client.observations) == 2
    task_observation, evidence_observation = client.observations
    assert task_observation.kwargs["name"] == "experiment-item-task"
    assert task_observation.input == {
        "role": "user",
        "content": "请重跑真实失败案例",
        "chars": 10,
        "truncated": False,
    }
    assert task_observation.output == client.dataset.task_output
    assert runtimes[0].closed is True
    assert client.dataset.task_output == {
        "role": "assistant",
        "content": "重跑后的回答",
        "chars": len("重跑后的回答"),
        "truncated": False,
        "terminal_status": "completed",
    }
    assert evidence_observation.kwargs["name"] == "runtime-regression-evidence"
    assert evidence_observation.kwargs["as_type"] == "span"
    evidence = evidence_observation.kwargs["input"]
    assert evidence["calls"] == []
    assert evidence["final_state"]["status"] == "completed"
    assert evidence["final_state"]["response"]["message"] == "重跑后的回答"
    assert evidence["infrastructure_error"] is None
    assert evidence_observation.output == client.dataset.task_output
    call = client.dataset.call
    assert call["name"] == "assistant-agent-runtime-regression"
    assert call["run_name"] == "runtime-regression-first-run"
    assert call["evaluators"] == []
    assert call["max_concurrency"] == 1
    assert call["metadata"] == {
        "evaluation_mode": "runtime_regression",
        "model": "production-model",
    }


def test_runtime_regression_rejects_truncated_ui_trace_input() -> None:
    client = _Client()
    client.dataset.items[0].input["truncated"] = True

    try:
        run_runtime_regression_experiment(
            client,
            RuntimeRegressionExperimentSettings(
                model="production-model",
                runtime_factory=_Runtime,
                run_name="runtime-regression-truncated",
            ),
        )
    except RuntimeError as exc:
        assert "truncated" in str(exc)
    else:
        raise AssertionError("truncated Langfuse item must not be replayed")


def test_runtime_regression_rejects_item_without_original_baseline_output() -> None:
    client = _Client()
    client.dataset.items[0].expected_output = None

    try:
        run_runtime_regression_experiment(
            client,
            RuntimeRegressionExperimentSettings(
                model="production-model",
                runtime_factory=_Runtime,
                run_name="runtime-regression-missing-baseline",
            ),
        )
    except RuntimeError as exc:
        assert "expected_output" in str(exc)
    else:
        raise AssertionError("regression item without a baseline must fail preflight")


def test_runtime_regression_waits_until_every_experiment_score_is_complete() -> None:
    class Experiments:
        def __init__(self) -> None:
            self.calls = 0

        def list_items(self, **kwargs):
            self.calls += 1
            names = ["assistant_agent.quality.grounding.experiment"]
            if self.calls == 2:
                names.extend(
                    [
                        "assistant_agent.quality.response_quality.experiment",
                        "assistant_agent.quality.regression_improvement.experiment",
                    ]
                )
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        experiment_item_id="runtime-item-1",
                        scores=[SimpleNamespace(name=name, value=True) for name in names],
                    )
                ]
            )

    experiments = Experiments()
    sleeps = []
    result = wait_for_runtime_regression_scores(
        SimpleNamespace(api=SimpleNamespace(experiments=experiments)),
        experiment_id="dataset-run-1",
        dataset_item_ids=("runtime-item-1",),
        timeout_seconds=2,
        poll_interval_seconds=1,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    assert experiments.calls == 2
    assert sleeps == [1]
    assert result == {
        "runtime-item-1": {
            "assistant_agent.quality.grounding.experiment": True,
            "assistant_agent.quality.regression_improvement.experiment": True,
            "assistant_agent.quality.response_quality.experiment": True,
        }
    }


def test_runtime_regression_collects_grounding_from_evidence_observation() -> None:
    class Experiments:
        def list_items(self, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        experiment_item_id="runtime-item-1",
                        trace_id="1" * 32,
                        scores=[
                            SimpleNamespace(
                                name="assistant_agent.quality.response_quality.experiment",
                                value=True,
                            ),
                            SimpleNamespace(
                                name=(
                                    "assistant_agent.quality."
                                    "regression_improvement.experiment"
                                ),
                                value=True,
                            ),
                        ],
                    )
                ]
            )

    observations = SimpleNamespace(
        get_many=lambda **kwargs: SimpleNamespace(
            data=[SimpleNamespace(id="evidence-span")]
        )
    )
    scores = SimpleNamespace(
        get_many_v3=lambda **kwargs: SimpleNamespace(
            data=[
                SimpleNamespace(
                    name="assistant_agent.quality.grounding.experiment",
                    value=True,
                    subject=SimpleNamespace(
                        kind="observation",
                        id="evidence-span",
                    ),
                )
            ]
        )
    )
    client = SimpleNamespace(
        api=SimpleNamespace(
            experiments=Experiments(),
            observations=observations,
            scores_v3=scores,
        )
    )

    assert wait_for_runtime_regression_scores(
        client,
        experiment_id="dataset-run-1",
        dataset_item_ids=("runtime-item-1",),
        timeout_seconds=0.1,
        poll_interval_seconds=1,
        sleep=lambda seconds: None,
    ) == {
        "runtime-item-1": {
            "assistant_agent.quality.grounding.experiment": True,
            "assistant_agent.quality.regression_improvement.experiment": True,
            "assistant_agent.quality.response_quality.experiment": True,
        }
    }


def test_runtime_regression_waits_for_nested_runtime_trace_completeness() -> None:
    class Experiments:
        def list_items(self, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        experiment_item_id="runtime-item-1",
                        trace_id="1" * 32,
                    )
                ]
            )

    class Observations:
        def __init__(self) -> None:
            self.calls = 0

        def get_many(self, **kwargs):
            self.calls += 1
            data = [
                SimpleNamespace(
                    id="run-span",
                    name="experiment-item-run",
                    type="SPAN",
                    parent_observation_id=None,
                ),
                SimpleNamespace(
                    id="task-span",
                    name="experiment-item-task",
                    type="SPAN",
                    parent_observation_id="run-span",
                ),
            ]
            if self.calls > 1:
                data.extend(
                    [
                        SimpleNamespace(
                            id="runtime-span",
                            name="agent.runtime",
                            type="SPAN",
                            parent_observation_id="task-span",
                        ),
                        SimpleNamespace(
                            id="llm-span",
                            name="llm.chat",
                            type="GENERATION",
                            parent_observation_id="runtime-span",
                        ),
                    ]
                )
            return SimpleNamespace(data=data, meta=SimpleNamespace(cursor=None))

    observations = Observations()
    sleeps = []
    client = SimpleNamespace(
        api=SimpleNamespace(
            experiments=Experiments(),
            observations=observations,
        )
    )

    result = wait_for_runtime_regression_trace_completeness(
        client,
        experiment_id="experiment-1",
        dataset_item_ids=("runtime-item-1",),
        timeout_seconds=2,
        poll_interval_seconds=1,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    assert observations.calls == 2
    assert sleeps == [1]
    assert result == {"runtime-item-1": "1" * 32}


def test_runtime_regression_rejects_orphan_runtime_trace() -> None:
    class Experiments:
        def list_items(self, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        experiment_item_id="runtime-item-1",
                        trace_id="1" * 32,
                    )
                ]
            )

    observations = SimpleNamespace(
        get_many=lambda **kwargs: SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="run-span",
                    name="experiment-item-run",
                    type="SPAN",
                    parent_observation_id=None,
                ),
                SimpleNamespace(
                    id="task-span",
                    name="experiment-item-task",
                    type="SPAN",
                    parent_observation_id="run-span",
                ),
                SimpleNamespace(
                    id="runtime-span",
                    name="agent.runtime",
                    type="SPAN",
                    parent_observation_id=None,
                ),
                SimpleNamespace(
                    id="llm-span",
                    name="llm.chat",
                    type="GENERATION",
                    parent_observation_id="runtime-span",
                ),
            ],
            meta=SimpleNamespace(cursor=None),
        )
    )
    client = SimpleNamespace(
        api=SimpleNamespace(
            experiments=Experiments(),
            observations=observations,
        )
    )

    try:
        wait_for_runtime_regression_trace_completeness(
            client,
            experiment_id="experiment-1",
            dataset_item_ids=("runtime-item-1",),
            timeout_seconds=0.1,
            poll_interval_seconds=1,
            sleep=lambda seconds: None,
        )
    except RuntimeError as exc:
        assert "agent.runtime parent" in str(exc)
    else:
        raise AssertionError("orphan Runtime trace must be infrastructure failure")


def test_runtime_regression_allows_additional_nested_workflow_runtime() -> None:
    class Experiments:
        def list_items(self, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        experiment_item_id="runtime-item-1",
                        trace_id="1" * 32,
                    )
                ]
            )

    observations = SimpleNamespace(
        get_many=lambda **kwargs: SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="run-span",
                    name="experiment-item-run",
                    type="SPAN",
                    parent_observation_id=None,
                ),
                SimpleNamespace(
                    id="task-span",
                    name="experiment-item-task",
                    type="SPAN",
                    parent_observation_id="run-span",
                ),
                SimpleNamespace(
                    id="runtime-span",
                    name="agent.runtime",
                    type="SPAN",
                    parent_observation_id="task-span",
                ),
                SimpleNamespace(
                    id="llm-span",
                    name="llm.chat",
                    type="GENERATION",
                    parent_observation_id="runtime-span",
                ),
                SimpleNamespace(
                    id="workflow-attempt",
                    name="workflow.attempt",
                    type="SPAN",
                    parent_observation_id="runtime-span",
                ),
                SimpleNamespace(
                    id="worker-runtime",
                    name="agent.runtime",
                    type="SPAN",
                    parent_observation_id="workflow-attempt",
                ),
            ],
            meta=SimpleNamespace(cursor=None),
        )
    )
    client = SimpleNamespace(
        api=SimpleNamespace(
            experiments=Experiments(),
            observations=observations,
        )
    )

    assert wait_for_runtime_regression_trace_completeness(
        client,
        experiment_id="experiment-1",
        dataset_item_ids=("runtime-item-1",),
        timeout_seconds=0.1,
        poll_interval_seconds=1,
        sleep=lambda seconds: None,
    ) == {"runtime-item-1": "1" * 32}


def test_runtime_regression_paginates_experiment_items() -> None:
    class Experiments:
        def __init__(self) -> None:
            self.cursors = []

        def list_items(self, **kwargs):
            cursor = kwargs.get("cursor")
            self.cursors.append(cursor)
            item_id = "runtime-item-1" if cursor is None else "runtime-item-2"
            trace_id = "1" * 32 if cursor is None else "2" * 32
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        experiment_item_id=item_id,
                        trace_id=trace_id,
                    )
                ],
                meta=SimpleNamespace(cursor="next-page" if cursor is None else None),
            )

    observations = SimpleNamespace(
        get_many=lambda **kwargs: SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="run-span",
                    name="experiment-item-run",
                    type="SPAN",
                    parent_observation_id=None,
                ),
                SimpleNamespace(
                    id="task-span",
                    name="experiment-item-task",
                    type="SPAN",
                    parent_observation_id="run-span",
                ),
                SimpleNamespace(
                    id="runtime-span",
                    name="agent.runtime",
                    type="SPAN",
                    parent_observation_id="task-span",
                ),
                SimpleNamespace(
                    id="llm-span",
                    name="llm.chat",
                    type="GENERATION",
                    parent_observation_id="runtime-span",
                ),
            ],
            meta=SimpleNamespace(cursor=None),
        )
    )
    experiments = Experiments()
    client = SimpleNamespace(
        api=SimpleNamespace(
            experiments=experiments,
            observations=observations,
        )
    )

    assert wait_for_runtime_regression_trace_completeness(
        client,
        experiment_id="experiment-1",
        dataset_item_ids=("runtime-item-1", "runtime-item-2"),
        timeout_seconds=0.1,
        poll_interval_seconds=1,
        sleep=lambda seconds: None,
    ) == {
        "runtime-item-1": "1" * 32,
        "runtime-item-2": "2" * 32,
    }
    assert experiments.cursors == [None, "next-page"]

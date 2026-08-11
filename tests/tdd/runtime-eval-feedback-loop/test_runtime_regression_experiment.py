from __future__ import annotations

from types import SimpleNamespace

from assistant_agent.runtime.requests import AgentResponse
from assistant_agent.runtime.state import AgentState
from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET
from evals.runtime_regression.experiment import (
    RuntimeRegressionExperimentSettings,
    run_runtime_regression_experiment,
    wait_for_runtime_regression_scores,
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
    def __init__(self) -> None:
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
                metadata={"source": "langfuse-ui"},
            )
        ]
        self.call = None
        self.task_output = None

    def run_experiment(self, **kwargs):
        self.call = kwargs
        self.task_output = kwargs["task"](item=self.items[0])
        return SimpleNamespace(
            run_name=kwargs["run_name"],
            dataset_run_id="dataset-run-1",
            dataset_run_url="http://langfuse/run/1",
        )


class _Client:
    def __init__(self) -> None:
        self.dataset = _Dataset()

    def get_dataset(self, name):
        assert name == RUNTIME_REGRESSION_DATASET
        return self.dataset


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
    assert runtimes[0].closed is True
    assert client.dataset.task_output["final_state"]["status"] == "completed"
    assert client.dataset.task_output["final_state"]["response"]["message"] == "重跑后的回答"
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


def test_runtime_regression_waits_until_every_experiment_score_is_complete() -> None:
    class Experiments:
        def __init__(self) -> None:
            self.calls = 0

        def list_items(self, **kwargs):
            self.calls += 1
            names = ["assistant_agent.quality.grounding.experiment"]
            if self.calls == 2:
                names.append("assistant_agent.quality.response_quality.experiment")
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
            "assistant_agent.quality.response_quality.experiment": True,
        }
    }

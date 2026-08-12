from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from langsmith.utils import LangSmithRateLimitError

from assistant_agent.evaluation.langsmith_trace import LangSmithExperimentBinding
from assistant_agent.observability.trace_context import (
    RuntimeExperimentTraceLink,
    RuntimeTraceContext,
)
from assistant_agent.runtime.requests import AgentResponse
from assistant_agent.runtime.state import AgentState
from evals.langsmith_runtime_regression import experiment


EXAMPLE_ID = UUID("01234567-89ab-cdef-0123-456789abcdef")


def _example(**overrides):
    values = {
        "id": EXAMPLE_ID,
        "inputs": {
            "role": "user",
            "content": "重跑问题",
            "chars": 4,
            "truncated": False,
        },
        "outputs": {
            "role": "assistant",
            "content": "原始失败回答",
            "chars": 6,
            "truncated": False,
            "terminal_status": "completed",
        },
        "metadata": {"active": True, "source_trace_id": "source-trace"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _binding() -> LangSmithExperimentBinding:
    link = RuntimeExperimentTraceLink(
        backend="langsmith",
        trace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        parent_run_id="11111111-2222-3333-4444-555555555555",
        experiment_id="99999999-8888-7777-6666-555555555555",
        project_name="run-name-12345678",
        reference_example_id=str(EXAMPLE_ID),
        parent_dotted_order=(
            "20260811T120000000000Z"
            "11111111-2222-3333-4444-555555555555"
        ),
    )
    return LangSmithExperimentBinding(
        project_id=link.experiment_id,
        project_name=link.project_name,
        trace_context=RuntimeTraceContext(
            trace_id="a" * 32,
            parent_span_id="1" * 16,
            experiment_link=link,
        ),
    )


class _Runtime:
    trace_store = SimpleNamespace(list_by_run=lambda _: [])

    def __init__(self) -> None:
        self.requests = []
        self.closed = False

    def run_state(self, request):
        self.requests.append(request)
        state = AgentState.from_request(
            request,
            run_id="run-regression",
            trace_id="a" * 32,
        )
        state.status = "completed"
        state.response = AgentResponse(message="修复后的回答")
        return state

    def close(self):
        self.closed = True
        return True


class _Result:
    experiment_id = UUID("99999999-8888-7777-6666-555555555555")
    experiment_name = "run-name"
    url = "https://smith.invalid/experiment"

    def __init__(self, example) -> None:
        self.rows = [
            {
                "example": example,
                "run": SimpleNamespace(id=UUID(int=1)),
                "evaluation_results": {"results": []},
            }
        ]

    def __iter__(self):
        return iter(self.rows)

    def get_dataset_id(self):
        return UUID(int=2)


class _Client:
    def __init__(self, examples=None) -> None:
        self.dataset = SimpleNamespace(id=UUID(int=2), name="dataset")
        self.examples = list(examples or [_example()])
        self.evaluate_call = None
        self.created_project = None

    def read_dataset(self, *, dataset_name):
        self.dataset_name = dataset_name
        return self.dataset

    def list_examples(self, *, dataset_id):
        assert dataset_id == self.dataset.id
        return iter(self.examples)

    def create_project(self, project_name, **kwargs):
        self.created_project = SimpleNamespace(
            id=UUID("99999999-8888-7777-6666-555555555555"),
            name=project_name,
            reference_dataset_id=kwargs["reference_dataset_id"],
            metadata=kwargs.get("metadata"),
        )
        return self.created_project

    def evaluate(self, target, /, **kwargs):
        self.evaluate_call = kwargs
        for example in kwargs["data"]:
            target(example.inputs)
        return _Result(self.examples[0])


def test_langsmith_experiment_replays_active_object_example(monkeypatch) -> None:
    client = _Client([_example(), _example(id=UUID(int=3), metadata={"active": False})])
    runtimes = []
    binding = _binding()
    monkeypatch.setattr(
        experiment,
        "current_langsmith_experiment_binding",
        lambda **_: binding,
    )

    def runtime_factory(received_binding):
        assert received_binding == binding
        runtime = _Runtime()
        runtimes.append(runtime)
        return runtime

    result = experiment.run_langsmith_runtime_regression_experiment(
        client,
        experiment.LangSmithRuntimeRegressionSettings(
            model="production-model",
            runtime_factory=runtime_factory,
            run_name="run-name",
            git_commit="abc123",
            max_concurrency=1,
        ),
    )

    assert result.example_ids == (str(EXAMPLE_ID),)
    assert result.run_ids == (str(UUID(int=1)),)
    assert client.evaluate_call["data"] == [client.examples[0]]
    assert client.evaluate_call["evaluators"] == []
    assert client.evaluate_call["blocking"] is True
    assert client.evaluate_call["max_concurrency"] == 1
    assert client.evaluate_call["experiment"] is client.created_project
    assert "experiment_prefix" not in client.evaluate_call
    assert runtimes[0].requests[0].text == "重跑问题"
    assert runtimes[0].closed is True


@pytest.mark.parametrize(
    "examples,match",
    [
        ([_example(outputs='{"role":"assistant"}')], "must be an object"),
        ([_example(inputs={"role": "user", "content": "", "truncated": False})], "no user content"),
        ([_example(inputs={"role": "user", "content": "x", "truncated": True})], "truncated"),
        ([_example(metadata={"active": False})], "no active examples"),
    ],
)
def test_langsmith_dataset_preflight_rejects_invalid_examples(examples, match) -> None:
    with pytest.raises(RuntimeError, match=match):
        experiment.inspect_langsmith_runtime_regression_dataset(_Client(examples))


def test_target_fails_closed_when_item_runtime_does_not_close(monkeypatch) -> None:
    binding = _binding()
    monkeypatch.setattr(
        experiment,
        "current_langsmith_experiment_binding",
        lambda **_: binding,
    )

    class Runtime(_Runtime):
        def close(self):
            return False

    with pytest.raises(RuntimeError, match="failed to close"):
        experiment.run_langsmith_runtime_regression_experiment(
            _Client(),
            experiment.LangSmithRuntimeRegressionSettings(
                model="production-model",
                runtime_factory=lambda _: Runtime(),
                run_name="run-name",
                git_commit="abc123",
            ),
        )


def test_completeness_waits_for_runtime_tree_and_all_feedback() -> None:
    required = experiment.REQUIRED_LANGSMITH_FEEDBACK_KEYS

    class Client:
        def __init__(self) -> None:
            self.feedback_calls = 0
            self.run_calls = []

        def list_runs(self, **kwargs):
            self.run_calls.append(kwargs)
            return iter(
                [
                    SimpleNamespace(
                        id=UUID(int=1),
                        parent_run_id=None,
                        name="experiment-item-task",
                        reference_example_id=EXAMPLE_ID,
                        trace_id=UUID(int=4),
                        inputs={"role": "user"},
                        outputs={"role": "assistant"},
                    ),
                    SimpleNamespace(
                        id=UUID(int=2),
                        parent_run_id=UUID(int=1),
                        name="agent.runtime",
                        reference_example_id=EXAMPLE_ID,
                        trace_id=UUID(int=4),
                        inputs={"role": "user"},
                        outputs={"role": "assistant"},
                    ),
                    SimpleNamespace(
                        id=UUID(int=3),
                        parent_run_id=UUID(int=2),
                        name="react.iteration",
                        reference_example_id=None,
                        trace_id=UUID(int=4),
                        inputs={"iteration": 1},
                        outputs={"status": "completed"},
                    ),
                    SimpleNamespace(
                        id=UUID(int=4),
                        parent_run_id=UUID(int=3),
                        name="llm.chat",
                        reference_example_id=None,
                        trace_id=UUID(int=4),
                        inputs={"messages": []},
                        outputs={"role": "assistant"},
                    ),
                ]
            )

        def list_feedback(self, **kwargs):
            self.feedback_calls += 1
            scores = (
                {key: (None if key == required[-1] else True) for key in required}
                if self.feedback_calls == 1
                else {key: True for key in required}
            )
            return iter(
                [
                    SimpleNamespace(run_id=UUID(int=1), key=key, score=score)
                    for key, score in scores.items()
                ]
            )

    client = Client()
    sleeps = []
    result = experiment.wait_for_langsmith_runtime_regression_completeness(
        client,
        experiment_id="experiment-id",
        example_ids=(str(EXAMPLE_ID),),
        timeout_seconds=2,
        poll_interval_seconds=1,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    assert result.run_ids == (str(UUID(int=1)),)
    assert set(result.feedback[str(EXAMPLE_ID)]) == set(required)
    assert sleeps == [1]
    assert len(client.run_calls) == 2
    assert all(call["project_id"] == "experiment-id" for call in client.run_calls)
    assert all("start_time" in call for call in client.run_calls)
    assert all("select" in call for call in client.run_calls)
    assert all("limit" not in call for call in client.run_calls)


def test_completeness_rejects_llm_sibling_outside_runtime_subtree() -> None:
    required = experiment.REQUIRED_LANGSMITH_FEEDBACK_KEYS

    class Client:
        def list_runs(self, **kwargs):
            return iter(
                [
                    SimpleNamespace(
                        id=UUID(int=1),
                        parent_run_id=None,
                        name="experiment-item-task",
                        reference_example_id=EXAMPLE_ID,
                        trace_id=UUID(int=4),
                        inputs={"role": "user"},
                        outputs={"role": "assistant"},
                    ),
                    SimpleNamespace(
                        id=UUID(int=2),
                        parent_run_id=UUID(int=1),
                        name="agent.runtime",
                        reference_example_id=EXAMPLE_ID,
                        trace_id=UUID(int=4),
                        inputs={"role": "user"},
                        outputs={"role": "assistant"},
                    ),
                    SimpleNamespace(
                        id=UUID(int=3),
                        parent_run_id=UUID(int=1),
                        name="llm.chat",
                        reference_example_id=None,
                        trace_id=UUID(int=4),
                        inputs={"messages": []},
                        outputs={"role": "assistant"},
                    ),
                ]
            )

        def list_feedback(self, **kwargs):
            return iter(
                [
                    SimpleNamespace(run_id=UUID(int=1), key=key, score=True)
                    for key in required
                ]
            )

    with pytest.raises(RuntimeError, match="llm.chat descendant"):
        experiment.wait_for_langsmith_runtime_regression_completeness(
            Client(),
            experiment_id="experiment-id",
            example_ids=(str(EXAMPLE_ID),),
            timeout_seconds=1,
            poll_interval_seconds=1,
            sleep=lambda _: None,
        )


def test_completeness_retries_bounded_langsmith_rate_limit() -> None:
    required = experiment.REQUIRED_LANGSMITH_FEEDBACK_KEYS

    class Client:
        def __init__(self) -> None:
            self.run_calls = 0

        def list_runs(self, **kwargs):
            self.run_calls += 1
            if self.run_calls == 1:
                raise LangSmithRateLimitError("rate limited")
            return iter(
                [
                    SimpleNamespace(
                        id=UUID(int=1),
                        parent_run_id=None,
                        name="experiment-item-task",
                        reference_example_id=EXAMPLE_ID,
                        inputs={"role": "user"},
                        outputs={"role": "assistant"},
                    ),
                    SimpleNamespace(
                        id=UUID(int=2),
                        parent_run_id=UUID(int=1),
                        name="agent.runtime",
                        reference_example_id=EXAMPLE_ID,
                    ),
                    SimpleNamespace(
                        id=UUID(int=3),
                        parent_run_id=UUID(int=2),
                        name="llm.chat",
                        reference_example_id=None,
                    ),
                ]
            )

        def list_feedback(self, **kwargs):
            return iter(
                [
                    SimpleNamespace(run_id=UUID(int=1), key=key, score=True)
                    for key in required
                ]
            )

    client = Client()
    sleeps = []
    result = experiment.wait_for_langsmith_runtime_regression_completeness(
        client,
        experiment_id="experiment-id",
        example_ids=(str(EXAMPLE_ID),),
        timeout_seconds=2,
        poll_interval_seconds=1,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    assert result.run_ids == (str(UUID(int=1)),)
    assert client.run_calls == 2
    assert sleeps == [1]


def test_completeness_sleep_never_exceeds_remaining_deadline() -> None:
    now = [100.0]
    sleeps = []

    class Client:
        def list_runs(self, **kwargs):
            return iter([])

        def list_feedback(self, **kwargs):
            return iter([])

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    with pytest.raises(RuntimeError, match="incomplete"):
        experiment.wait_for_langsmith_runtime_regression_completeness(
            Client(),
            experiment_id="experiment-id",
            example_ids=(str(EXAMPLE_ID),),
            timeout_seconds=1.5,
            poll_interval_seconds=1,
            sleep=sleep,
            clock=lambda: now[0],
        )

    assert sleeps == [1, 0.5]

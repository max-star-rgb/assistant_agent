from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from langsmith.utils import LangSmithRateLimitError

from evals.langsmith_runtime_regression import experiment


EXAMPLE_ID = UUID("01234567-89ab-cdef-0123-456789abcdef")
TRACE_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


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


class _DatasetClient:
    def __init__(self, examples=None) -> None:
        self.dataset = SimpleNamespace(id=UUID(int=20), name="dataset")
        self.examples = list(examples or [_example()])

    def read_dataset(self, *, dataset_name):
        return self.dataset

    def list_examples(self, *, dataset_id):
        return iter(self.examples)


@pytest.mark.parametrize(
    "examples,match",
    [
        ([_example(outputs='{"role":"assistant"}')], "must be an object"),
        (
            [_example(inputs={"role": "user", "content": "", "truncated": False})],
            "no user content",
        ),
        (
            [_example(inputs={"role": "user", "content": "x", "truncated": True})],
            "truncated",
        ),
        ([_example(metadata={"active": False})], "no active examples"),
    ],
)
def test_langsmith_dataset_preflight_rejects_invalid_examples(examples, match) -> None:
    with pytest.raises(RuntimeError, match=match):
        experiment.inspect_langsmith_runtime_regression_dataset(
            _DatasetClient(examples)
        )


def _run(run_id: int, *, name: str, parent: int | None, run_type="chain"):
    return SimpleNamespace(
        id=UUID(int=run_id),
        parent_run_id=UUID(int=parent) if parent is not None else None,
        name=name,
        run_type=run_type,
        reference_example_id=EXAMPLE_ID if parent is None else None,
        trace_id=TRACE_ID,
        inputs={"value": "input"},
        outputs={"value": "output"},
    )


def _native_runs():
    return [
        _run(1, name="experiment-item-task", parent=None),
        _run(2, name="AssistantTurnGraph", parent=1),
        _run(3, name="assistant", parent=2),
        _run(4, name="llm.chat", parent=3, run_type="llm"),
        _run(5, name="compose_response", parent=2),
    ]


def test_completeness_waits_for_native_tree_and_all_feedback() -> None:
    required = experiment.REQUIRED_LANGSMITH_FEEDBACK_KEYS

    class Client:
        def __init__(self) -> None:
            self.feedback_calls = 0
            self.run_calls = []

        def list_runs(self, **kwargs):
            self.run_calls.append(kwargs)
            return iter(_native_runs())

        def list_feedback(self, **kwargs):
            self.feedback_calls += 1
            scores = (
                {key: (None if key == required[-1] else True) for key in required}
                if self.feedback_calls == 1
                else {key: True for key in required}
            )
            return iter(
                SimpleNamespace(run_id=UUID(int=1), key=key, score=score)
                for key, score in scores.items()
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
    assert all("run_type" in call["select"] for call in client.run_calls)
    assert all("limit" not in call for call in client.run_calls)
    assert all("start_time" not in call for call in client.run_calls)


def test_completeness_retries_bounded_langsmith_rate_limit() -> None:
    required = experiment.REQUIRED_LANGSMITH_FEEDBACK_KEYS

    class Client:
        def __init__(self) -> None:
            self.run_calls = 0

        def list_runs(self, **kwargs):
            self.run_calls += 1
            if self.run_calls == 1:
                raise LangSmithRateLimitError("rate limited")
            return iter(_native_runs())

        def list_feedback(self, **kwargs):
            return iter(
                SimpleNamespace(run_id=UUID(int=1), key=key, score=True)
                for key in required
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


def test_completeness_rejects_duplicate_feedback_for_one_example() -> None:
    required = experiment.REQUIRED_LANGSMITH_FEEDBACK_KEYS

    class Client:
        def list_runs(self, **_kwargs):
            return iter(_native_runs())

        def list_feedback(self, **_kwargs):
            return iter(
                [
                    SimpleNamespace(run_id=UUID(int=1), key=key, score=True)
                    for key in required
                ]
                + [
                    SimpleNamespace(
                        run_id=UUID(int=1),
                        key=required[0],
                        score=False,
                    )
                ]
            )

    with pytest.raises(RuntimeError, match="duplicate feedback"):
        experiment.wait_for_langsmith_runtime_regression_completeness(
            Client(),
            experiment_id="experiment-id",
            example_ids=(str(EXAMPLE_ID),),
            timeout_seconds=0.01,
            poll_interval_seconds=0.01,
            sleep=lambda _seconds: None,
            clock=iter((0.0, 0.0, 0.02)).__next__,
        )


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

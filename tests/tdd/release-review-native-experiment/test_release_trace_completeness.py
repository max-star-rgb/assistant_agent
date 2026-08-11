from __future__ import annotations

from types import SimpleNamespace

import pytest

from evals.release_review.langfuse_backend import _persisted_scores
from evals.release_review.report import CANONICAL_TASK_SCORES


def _observation(identifier, name, parent):
    return SimpleNamespace(
        id=identifier,
        name=name,
        type="GENERATION" if name == "llm.chat" else "SPAN",
        parent_observation_id=parent,
    )


class _Scores:
    def get_many_v3(self, **kwargs):
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    name=name,
                    value=True,
                    data_type="BOOLEAN",
                    subject=SimpleNamespace(
                        kind="observation",
                        trace_id="1" * 32,
                        id="task-span",
                    ),
                )
                for name in CANONICAL_TASK_SCORES
            ]
        )


def test_release_score_audit_rejects_runtime_outside_experiment_task() -> None:
    observations = [
        _observation("run-span", "experiment-item-run", None),
        _observation("task-span", "experiment-item-task", "run-span"),
        _observation("runtime-span", "agent.runtime", None),
        _observation("llm-span", "llm.chat", "runtime-span"),
    ]

    class Observations:
        def get_many(self, **kwargs):
            selected = (
                [item for item in observations if item.name == kwargs["name"]]
                if "name" in kwargs
                else observations
            )
            return SimpleNamespace(
                data=selected,
                meta=SimpleNamespace(cursor=None),
            )

    client = SimpleNamespace(
        api=SimpleNamespace(
            observations=Observations(),
            scores_v3=_Scores(),
        )
    )

    with pytest.raises(RuntimeError, match="agent.runtime parent"):
        _persisted_scores(
            client,
            "1" * 32,
            attempts=1,
            retry_delay_seconds=0,
        )


def test_release_score_audit_accepts_complete_nested_runtime_trace() -> None:
    observations = [
        _observation("run-span", "experiment-item-run", None),
        _observation("task-span", "experiment-item-task", "run-span"),
        _observation("runtime-span", "agent.runtime", "task-span"),
        _observation("llm-span", "llm.chat", "runtime-span"),
    ]
    client = SimpleNamespace(
        api=SimpleNamespace(
            observations=SimpleNamespace(
                get_many=lambda **kwargs: SimpleNamespace(
                    data=observations,
                    meta=SimpleNamespace(cursor=None),
                )
            ),
            scores_v3=_Scores(),
        )
    )

    assert _persisted_scores(
        client,
        "1" * 32,
        attempts=1,
        retry_delay_seconds=0,
    ) == {name: True for name in CANONICAL_TASK_SCORES}

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evals.runtime_regression.dataset import (
    RUNTIME_REGRESSION_DATASET,
    promote_failed_score,
)


TRACE_ID = "trace-runtime-failure"
SCORE_ID = "score-grounding-failure"
TARGET_OBSERVATION_ID = "observation-llm-chat"
ROOT_OBSERVATION_ID = "observation-agent-runtime"


class _Scores:
    def __init__(self, selected_score: dict) -> None:
        self.selected_score = selected_score

    def get_many_v3(self, **kwargs):
        if kwargs.get("id") == SCORE_ID:
            return SimpleNamespace(data=[self.selected_score], meta=SimpleNamespace(cursor=None))
        assert kwargs.get("trace_id") == TRACE_ID
        return SimpleNamespace(
            data=[
                self.selected_score,
                {
                    "id": "score-response-quality-pass",
                    "name": "assistant_agent.quality.response_quality",
                    "dataType": "BOOLEAN",
                    "value": True,
                    "source": "EVAL",
                    "subject": {
                        "kind": "observation",
                        "id": TARGET_OBSERVATION_ID,
                        "traceId": TRACE_ID,
                    },
                },
            ],
            meta=SimpleNamespace(cursor=None),
        )


class _Observations:
    def get_many(self, **kwargs):
        assert kwargs["trace_id"] == TRACE_ID
        assert kwargs["fields"] == "core,basic,io"
        return SimpleNamespace(
            data=[
                {
                    "id": TARGET_OBSERVATION_ID,
                    "traceId": TRACE_ID,
                    "name": "llm.chat",
                    "type": "GENERATION",
                    "parentObservationId": "iteration",
                    "input": json.dumps({"input": [{"role": "user", "content": "原始请求"}]}),
                    "output": json.dumps({"role": "assistant", "content": "无证据断言"}),
                },
                {
                    "id": ROOT_OBSERVATION_ID,
                    "traceId": TRACE_ID,
                    "name": "agent.runtime",
                    "type": "SPAN",
                    "parentObservationId": None,
                    "input": json.dumps(
                        {
                            "role": "user",
                            "content": "请验证这个真实失败案例",
                            "truncated": False,
                        }
                    ),
                    "output": json.dumps(
                        {
                            "role": "assistant",
                            "content": "无证据断言",
                            "terminal_status": "completed",
                            "truncated": False,
                        }
                    ),
                },
            ],
            meta=SimpleNamespace(cursor=None),
        )


class _Client:
    def __init__(self, selected_score: dict | None = None) -> None:
        score = selected_score or {
            "id": SCORE_ID,
            "name": "assistant_agent.quality.grounding",
            "dataType": "BOOLEAN",
            "value": False,
            "source": "EVAL",
            "subject": {
                "kind": "observation",
                "id": TARGET_OBSERVATION_ID,
                "traceId": TRACE_ID,
            },
        }
        self.api = SimpleNamespace(
            scores_v3=_Scores(score),
            observations=_Observations(),
        )
        self.datasets: list[dict] = []
        self.items: list[dict] = []

    def create_dataset(self, **kwargs):
        self.datasets.append(kwargs)

    def create_dataset_item(self, **kwargs):
        self.items.append(kwargs)
        return SimpleNamespace(id=kwargs["id"])


def test_promote_failed_score_writes_trace_linked_runtime_regression_item() -> None:
    client = _Client()

    result = promote_failed_score(
        client,
        score_id=SCORE_ID,
        reviewed_by="codex",
    )

    assert result.dataset_name == RUNTIME_REGRESSION_DATASET
    assert result.failed_score_names == ("assistant_agent.quality.grounding",)
    assert result.source_trace_id == TRACE_ID
    assert result.source_observation_id == TARGET_OBSERVATION_ID
    assert len(client.datasets) == 1
    assert client.datasets[0]["name"] == RUNTIME_REGRESSION_DATASET
    assert len(client.items) == 1
    item = client.items[0]
    assert item["dataset_name"] == RUNTIME_REGRESSION_DATASET
    assert item["id"] == result.dataset_item_id
    assert item["input"] == {"request": "请验证这个真实失败案例"}
    assert item["expected_output"] == {
        "required_scores": {"assistant_agent.quality.grounding": True}
    }
    assert item["source_trace_id"] == TRACE_ID
    assert item["source_observation_id"] == TARGET_OBSERVATION_ID
    assert item["metadata"]["source_score_id"] == SCORE_ID
    assert item["metadata"]["failed_score_names"] == [
        "assistant_agent.quality.grounding"
    ]
    assert item["metadata"]["reviewed_by"] == "codex"
    assert item["metadata"]["root_observation_id"] == ROOT_OBSERVATION_ID
    assert getattr(item["status"], "value", item["status"]) == "ACTIVE"


def test_promote_failed_score_accepts_langfuse_sdk_snake_case_models() -> None:
    """Would fail if API aliases worked in fixtures but not on real SDK models."""

    client = _Client(
        SimpleNamespace(
            id=SCORE_ID,
            name="assistant_agent.quality.grounding",
            data_type="BOOLEAN",
            value=False,
            source="EVAL",
            subject=SimpleNamespace(
                kind="observation",
                id=TARGET_OBSERVATION_ID,
                trace_id=TRACE_ID,
            ),
        )
    )

    result = promote_failed_score(
        client,
        score_id=SCORE_ID,
        reviewed_by="codex",
    )

    assert result.source_trace_id == TRACE_ID
    assert result.failed_score_names == ("assistant_agent.quality.grounding",)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"value": True}, "must be false"),
        ({"name": "unrelated.score"}, "canonical quality Score"),
        ({"source": "API"}, "source must be EVAL"),
        ({"subject": {"kind": "trace", "id": "trace", "traceId": TRACE_ID}}, "observation"),
    ],
)
def test_promote_failed_score_rejects_non_actionable_score(change, message) -> None:
    base = _Client().api.scores_v3.selected_score
    client = _Client({**base, **change})

    with pytest.raises(ValueError, match=message):
        promote_failed_score(client, score_id=SCORE_ID, reviewed_by="codex")

    assert client.items == []

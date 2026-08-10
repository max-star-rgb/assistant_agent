from types import SimpleNamespace

from evals.agent.langfuse_backend import verify_persisted_dimension_scores


EXPECTED_SCORE_NAMES = {
    "assistant_agent.quality.task_conformance",
    "assistant_agent.quality.grounding",
    "assistant_agent.quality.response_quality",
}


class ObservationResource:
    def __init__(self) -> None:
        self.query: dict[str, object] | None = None

    def get_many(self, **query: object) -> SimpleNamespace:
        self.query = query
        return SimpleNamespace(data=[SimpleNamespace(id="observation-v4")])


class LegacyObservationResource:
    def get_many(self, **query: object) -> SimpleNamespace:
        raise AssertionError(f"legacy observation API called: {query}")


class ScoreResource:
    def __init__(self) -> None:
        self.query: dict[str, object] | None = None

    def get_many_v3(self, **query: object) -> SimpleNamespace:
        self.query = query
        trace_id = query["trace_id"]
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    name=name,
                    data_type="BOOLEAN",
                    value=True,
                    subject=SimpleNamespace(
                        kind="observation",
                        trace_id=trace_id,
                        id="observation-v4",
                    ),
                )
                for name in EXPECTED_SCORE_NAMES
            ]
        )


def test_persisted_score_verification_uses_v4_observations_api() -> None:
    observations = ObservationResource()
    scores = ScoreResource()
    client = SimpleNamespace(
        flush=lambda: None,
        api=SimpleNamespace(
            observations=observations,
            legacy=SimpleNamespace(observations_v1=LegacyObservationResource()),
            scores_v3=scores,
        ),
    )
    result = SimpleNamespace(
        item_results=[SimpleNamespace(trace_id="trace-v4")],
    )

    persisted = verify_persisted_dimension_scores(
        client,
        result,
        attempts=1,
        retry_delay_seconds=0,
    )

    assert observations.query == {
        "trace_id": "trace-v4",
        "name": "experiment-item-task",
        "type": "SPAN",
        "limit": 2,
    }
    assert scores.query == {
        "trace_id": "trace-v4",
        "observation_id": "observation-v4",
        "name": ",".join(sorted(EXPECTED_SCORE_NAMES)),
        "fields": "subject",
        "limit": 100,
    }
    assert persisted == [
        {
            "task_conformance": True,
            "grounding": True,
            "response_quality": True,
        }
    ]

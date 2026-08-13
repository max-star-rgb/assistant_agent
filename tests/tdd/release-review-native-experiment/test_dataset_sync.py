from __future__ import annotations

from types import SimpleNamespace

from langsmith.utils import LangSmithNotFoundError

from evals.release_review.contracts import ReleaseScenario
from evals.release_review.langsmith_backend import (
    GIT_EXAMPLE_OWNER,
    RELEASE_REVIEW_DATASET,
    sync_langsmith_examples,
)


def _scenario(scenario_id: str, *, repetitions: int = 1) -> ReleaseScenario:
    return ReleaseScenario.model_validate(
        {
            "id": scenario_id,
            "phase": "decision",
            "capability": "probe",
            "risk": "high",
            "request": f"request for {scenario_id}",
            "repetitions": repetitions,
            "tool_contract": {"required": ["probe_tool"]},
            "fixtures": {"probe_tool": [{"success": True, "data": {}}]},
            "state_assertions": [{"path": "status", "equals": "completed"}],
        }
    )


class FakeClient:
    def __init__(self, existing: list[object] | None = None, *, missing=False) -> None:
        self.dataset = SimpleNamespace(id="dataset-sentinel")
        self.existing = list(existing or [])
        self.missing = missing
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    def read_dataset(self, *, dataset_name: str):
        assert dataset_name == RELEASE_REVIEW_DATASET
        if self.missing:
            self.missing = False
            raise LangSmithNotFoundError("missing")
        return self.dataset

    def create_dataset(self, name: str, **_kwargs):
        assert name == RELEASE_REVIEW_DATASET
        return self.dataset

    def list_examples(self, *, dataset_id: str):
        assert dataset_id == self.dataset.id
        return list(self.existing)

    def create_example(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=str(kwargs["example_id"]))

    def update_example(self, example_id: str, **kwargs):
        self.updated.append((str(example_id), kwargs))


def test_sync_creates_dataset_and_expands_repetitions() -> None:
    client = FakeClient(missing=True)

    result = sync_langsmith_examples(
        client, [_scenario("critical_probe", repetitions=2)], "git-sentinel"
    )

    assert result.dataset_name == RELEASE_REVIEW_DATASET
    assert len(result.active_example_ids) == 2
    assert len(client.created) == 2
    assert client.created[0]["inputs"] == {
        "scenario_id": "critical_probe",
        "request": "request for critical_probe",
    }
    assert client.created[0]["metadata"]["git_commit"] == "git-sentinel"
    assert client.created[0]["metadata"]["owner"] == GIT_EXAMPLE_OWNER


def test_sync_updates_current_and_archives_only_stale_owned_examples() -> None:
    current = SimpleNamespace(
        id="current-example",
        metadata={
            "owner": GIT_EXAMPLE_OWNER,
            "scenario_id": "current",
            "repetition": 1,
            "active": True,
        },
    )
    stale = SimpleNamespace(
        id="stale-example",
        metadata={
            "owner": GIT_EXAMPLE_OWNER,
            "scenario_id": "removed",
            "repetition": 1,
            "active": True,
        },
    )
    foreign = SimpleNamespace(
        id="foreign-example",
        metadata={"owner": "someone_else", "scenario_id": "foreign", "repetition": 1},
    )
    client = FakeClient([current, stale, foreign])

    result = sync_langsmith_examples(client, [_scenario("current")], "git-sentinel")

    assert result.active_example_ids == ("current-example",)
    assert result.archived_example_ids == ("stale-example",)
    assert [item[0] for item in client.updated] == ["current-example", "stale-example"]
    assert client.updated[-1][1]["metadata"]["active"] is False

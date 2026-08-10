from __future__ import annotations

from types import SimpleNamespace

from evals.release_review.contracts import ReleaseScenario
from evals.release_review.sync_dataset import (
    RELEASE_REVIEW_DATASET,
    sync_release_dataset,
)


def _scenario(
    scenario_id: str, *, repetitions: int = 1, risk: str = "high"
) -> ReleaseScenario:
    return ReleaseScenario.model_validate(
        {
            "id": scenario_id,
            "phase": "decision",
            "capability": "probe",
            "risk": risk,
            "request": f"request for {scenario_id}",
            "repetitions": repetitions,
            "tool_contract": {
                "required": ["probe_tool"],
                "allowed": [],
                "forbidden": [],
            },
            "fixtures": {"probe_tool": [{"success": True, "data": {}}]},
            "state_assertions": [{"path": "status", "equals": "completed"}],
        }
    )


class FakeClient:
    def __init__(self, existing: list[object] | None = None) -> None:
        self.existing = list(existing or [])
        self.datasets: list[dict] = []
        self.items: list[dict] = []

    def create_dataset(self, **kwargs):
        self.datasets.append(kwargs)

    def get_dataset(self, name: str):
        assert name == RELEASE_REVIEW_DATASET
        return SimpleNamespace(items=self.existing)

    def create_dataset_item(self, **kwargs):
        self.items.append(kwargs)


def test_sync_uses_one_dataset_and_expands_repetitions() -> None:
    client = FakeClient()

    result = sync_release_dataset(
        client,
        [_scenario("critical_probe", repetitions=2, risk="critical"), _scenario("high_probe")],
        "git-sentinel",
    )

    assert [dataset["name"] for dataset in client.datasets] == [
        "assistant-agent-release-review"
    ]
    active = [item for item in client.items if item.get("status") != "ARCHIVED"]
    assert [item["id"] for item in active] == [
        "assistant-agent-release-review__critical_probe__r1",
        "assistant-agent-release-review__critical_probe__r2",
        "assistant-agent-release-review__high_probe__r1",
    ]
    assert active[0]["input"] == {
        "scenario_id": "critical_probe",
        "request": "request for critical_probe",
    }
    assert active[0]["expected_output"] == {
        "tool_contract": {
            "required": ["probe_tool"],
            "allowed": [],
            "forbidden": [],
            "arguments": [],
            "sequence": {"before": [], "before_final_response": []},
        },
        "state_assertions": [{"path": "status", "equals": "completed"}],
    }
    assert active[0]["metadata"]["git_commit"] == "git-sentinel"
    assert active[0]["metadata"]["repetition"] == 1
    assert result.active_item_ids == tuple(item["id"] for item in active)


def test_sync_archives_only_stale_git_owned_items() -> None:
    stale = SimpleNamespace(
        id="assistant-agent-release-review__removed__r1",
        input={"scenario_id": "removed", "request": "old"},
        expected_output={},
        metadata={"owner": "assistant_agent_release_review", "scenario_id": "removed"},
        status="ACTIVE",
    )
    foreign = SimpleNamespace(
        id="foreign-item",
        input={},
        expected_output=None,
        metadata={"owner": "someone_else"},
        status="ACTIVE",
    )
    client = FakeClient([stale, foreign])

    result = sync_release_dataset(client, [_scenario("current")], "git-sentinel")

    archived = [item for item in client.items if item.get("status") == "ARCHIVED"]
    assert [item["id"] for item in archived] == [stale.id]
    assert result.archived_item_ids == (stale.id,)
    assert all(item["id"] != foreign.id for item in client.items)

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from evals.release_review.langsmith_backend import sync_langsmith_examples
from evals.release_review.loader import load_scenarios


SCENARIO_ROOT = Path(__file__).resolve().parents[3] / "evals/release_review/scenarios"


class SyncClient:
    def __init__(self) -> None:
        self.dataset = SimpleNamespace(id="dataset-sentinel")
        self.examples: list[dict] = []

    def read_dataset(self, *, dataset_name):
        return self.dataset

    def list_examples(self, *, dataset_id):
        return []

    def create_example(self, **kwargs):
        self.examples.append(kwargs)
        return SimpleNamespace(id=str(kwargs["example_id"]))


def test_initial_scenario_inventory_and_release_safety_contracts() -> None:
    scenarios = load_scenarios(SCENARIO_ROOT)
    decisions = [item for item in scenarios if item.phase == "decision"]
    staging = [item for item in scenarios if item.phase == "staging"]

    assert len(decisions) == 8
    assert len(staging) == 3
    assert all(item.repetitions == 2 for item in decisions if item.risk == "critical")
    assert all(
        item.staging is not None and item.staging.cleanup == "required"
        for item in staging
        if any("create" in tool for tool in item.tool_contract.required)
    )
    assert {
        tool for item in scenarios for tool in item.tool_contract.required
    } == {"calendar_create", "calendar_search", "mcp.amap_maps.maps_geo"}


def test_langsmith_example_count_equals_declared_repetitions() -> None:
    scenarios = load_scenarios(SCENARIO_ROOT)
    client = SyncClient()

    result = sync_langsmith_examples(client, scenarios, "git-sentinel")

    assert len(result.active_example_ids) == sum(item.repetitions for item in scenarios)
    assert len(client.examples) == len(result.active_example_ids)

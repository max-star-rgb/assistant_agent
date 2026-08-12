from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from evals.release_review.loader import load_scenarios
from evals.release_review.sync_dataset import sync_release_dataset


SCENARIO_ROOT = (
    Path(__file__).resolve().parents[3] / "evals" / "release_review" / "scenarios"
)


class SyncClient:
    def __init__(self) -> None:
        self.items = []

    def create_dataset(self, **kwargs):
        pass

    def get_dataset(self, name):
        return SimpleNamespace(items=[])

    def create_dataset_item(self, **kwargs):
        self.items.append(kwargs)


def test_initial_scenario_inventory_and_release_safety_contracts() -> None:
    scenarios = load_scenarios(SCENARIO_ROOT)
    decisions = [item for item in scenarios if item.phase == "decision"]
    staging = [item for item in scenarios if item.phase == "staging"]

    assert len(decisions) == 8
    assert len(staging) == 3
    assert {item.id for item in scenarios} == {
        "deep_research_admission",
        "simple_request_no_workflow",
        "deep_research_constraints",
        "tool_failure_no_repeat",
        "correct_tool_among_candidates",
        "write_requires_precondition",
        "wait_for_tool_result",
        "no_unobserved_result_claim",
        "staging_deep_research_workflow",
        "staging_amap_read_chain",
        "staging_calendar_write_read_cleanup",
    }
    assert all(
        item.repetitions == 2
        for item in decisions
        if item.risk == "critical"
    )
    assert all(
        item.staging is not None and item.staging.cleanup == "required"
        for item in staging
        if any("create" in tool for tool in item.tool_contract.required)
    )
    required_tools = {
        tool
        for item in scenarios
        for tool in item.tool_contract.required
    }
    assert required_tools == {
        "calendar_create",
        "calendar_search",
        "mcp.amap_maps.maps_geo",
    }
    assert all(
        item.assistant_mode == "deep_research"
        for item in scenarios
        if item.id.startswith("deep_research_")
        or item.id == "staging_deep_research_workflow"
    )
    by_id = {item.id: item for item in scenarios}
    assert by_id["correct_tool_among_candidates"].tool_contract.arguments == ()
    assert by_id["tool_failure_no_repeat"].tool_contract.arguments == ()
    for item in scenarios:
        lowered = item.request.lower()
        assert "fixture" not in lowered
        assert "scenario" not in lowered
        assert all(tool.lower() not in lowered for tool in item.tool_contract.required)


def test_dataset_item_count_equals_declared_repetitions() -> None:
    scenarios = load_scenarios(SCENARIO_ROOT)
    client = SyncClient()

    result = sync_release_dataset(client, scenarios, "git-sentinel")

    assert len(result.active_item_ids) == sum(item.repetitions for item in scenarios)

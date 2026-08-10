from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from evals.release_review.loader import load_scenario, load_scenarios, scenario_hash


DECISION_SCENARIO = {
    "id": "deep_research_admission",
    "phase": "decision",
    "capability": "deep_research",
    "risk": "critical",
    "request": "请深入研究这个主题，并给出可核验的结论。",
    "repetitions": 2,
    "tool_contract": {
        "required": ["workflow.deep_research"],
        "allowed": ["web.search"],
        "forbidden": ["calendar.create"],
        "arguments": [
            {"tool": "workflow.deep_research", "path": "topic", "contains": "主题"},
        ],
        "sequence": {
            "before": [["workflow.deep_research", "web.search"]],
            "before_final_response": ["workflow.deep_research"],
        },
    },
    "fixtures": {
        "workflow.deep_research": [
            {"success": True, "data": {"workflow_id": "fixture-1"}},
        ],
    },
    "state_assertions": [{"path": "status", "equals": "completed"}],
}

STAGING_SCENARIO = {
    "id": "staging_amap_read_chain",
    "phase": "staging",
    "capability": "maps",
    "risk": "high",
    "request": "查询杭州西湖的位置。",
    "tool_contract": {
        "required": ["mcp.amap_maps.maps_geo"],
        "allowed": [],
        "forbidden": [],
    },
    "staging": {"resource_profile": "amap_readonly", "cleanup": "skipped"},
}


def _write_yaml(root: Path, payload: dict, name: str = "scenario.yaml") -> Path:
    path = root / name
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_loads_decision_and_staging_contracts(tmp_path: Path) -> None:
    decision = load_scenario(_write_yaml(tmp_path, DECISION_SCENARIO, "decision.yaml"))
    staging = load_scenario(_write_yaml(tmp_path, STAGING_SCENARIO, "staging.yaml"))

    assert decision.id == "deep_research_admission"
    assert decision.tool_contract.arguments[0].contains == "主题"
    assert decision.fixtures["workflow.deep_research"][0].data == {
        "workflow_id": "fixture-1"
    }
    assert staging.staging is not None
    assert staging.staging.resource_profile == "amap_readonly"


def test_rejects_unknown_fields_with_filename(tmp_path: Path) -> None:
    payload = deepcopy(DECISION_SCENARIO)
    payload["unexpected"] = True

    with pytest.raises(ValueError, match=r"unknown\.yaml:.*unexpected"):
        load_scenario(_write_yaml(tmp_path, payload, "unknown.yaml"))


def test_load_scenarios_rejects_duplicate_ids(tmp_path: Path) -> None:
    _write_yaml(tmp_path, DECISION_SCENARIO, "one.yaml")
    _write_yaml(tmp_path, DECISION_SCENARIO, "two.yaml")

    with pytest.raises(ValueError, match=r"duplicate scenario id.*deep_research_admission"):
        load_scenarios(tmp_path)


def test_rejects_required_forbidden_conflict(tmp_path: Path) -> None:
    payload = deepcopy(DECISION_SCENARIO)
    payload["tool_contract"]["forbidden"] = ["workflow.deep_research"]

    with pytest.raises(ValueError, match=r"conflict\.yaml:.*required.*forbidden"):
        load_scenario(_write_yaml(tmp_path, payload, "conflict.yaml"))


def test_decision_requires_fixture_for_required_tools(tmp_path: Path) -> None:
    payload = deepcopy(DECISION_SCENARIO)
    payload["fixtures"] = {}

    with pytest.raises(ValueError, match=r"missing-fixture\.yaml:.*missing fixtures"):
        load_scenario(_write_yaml(tmp_path, payload, "missing-fixture.yaml"))


def test_staging_requires_cleanup_contract(tmp_path: Path) -> None:
    payload = deepcopy(STAGING_SCENARIO)
    del payload["staging"]["cleanup"]

    with pytest.raises(ValueError, match=r"missing-cleanup\.yaml:.*cleanup"):
        load_scenario(_write_yaml(tmp_path, payload, "missing-cleanup.yaml"))


@pytest.mark.parametrize("operator", ["equals", "contains", "gte", "exists", "length"])
def test_argument_assertion_accepts_exactly_one_operator(
    tmp_path: Path, operator: str
) -> None:
    payload = deepcopy(DECISION_SCENARIO)
    assertion = {"tool": "workflow.deep_research", "path": "topic", operator: 1}
    if operator == "exists":
        assertion[operator] = True
    payload["tool_contract"]["arguments"] = [assertion]

    scenario = load_scenario(_write_yaml(tmp_path, payload, f"{operator}.yaml"))

    assert getattr(scenario.tool_contract.arguments[0], operator) == assertion[operator]


def test_argument_assertion_rejects_multiple_operators(tmp_path: Path) -> None:
    payload = deepcopy(DECISION_SCENARIO)
    payload["tool_contract"]["arguments"] = [
        {
            "tool": "workflow.deep_research",
            "path": "topic",
            "equals": "主题",
            "contains": "主",
        }
    ]

    with pytest.raises(ValueError, match=r"operators\.yaml:.*exactly one"):
        load_scenario(_write_yaml(tmp_path, payload, "operators.yaml"))


@pytest.mark.parametrize(
    ("phase", "risk", "repetitions"),
    [("decision", "critical", 1), ("decision", "high", 3)],
)
def test_repetition_policy_is_enforced(
    tmp_path: Path, phase: str, risk: str, repetitions: int
) -> None:
    payload = deepcopy(DECISION_SCENARIO)
    payload.update(phase=phase, risk=risk, repetitions=repetitions)

    with pytest.raises(ValueError, match=r"repetitions\.yaml:.*repetitions"):
        load_scenario(_write_yaml(tmp_path, payload, "repetitions.yaml"))


def test_scenario_hash_is_stable_for_equivalent_yaml(tmp_path: Path) -> None:
    first = load_scenario(_write_yaml(tmp_path, DECISION_SCENARIO, "first.yaml"))
    reordered = dict(reversed(list(DECISION_SCENARIO.items())))
    second = load_scenario(_write_yaml(tmp_path, reordered, "second.yaml"))

    assert scenario_hash(first) == scenario_hash(second)
    assert len(scenario_hash(first)) == 64

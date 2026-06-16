import json
import re
from pathlib import Path
from typing import Any


SCENARIO_PATH = Path("demo_data/scenarios/e2e_demo_scenarios.json")
REQUIRED_SCENARIOS = {
    "text_chat",
    "text_image_generation",
    "image_understanding",
    "video_understanding",
    "product_search_compare",
    "image_to_product_search_compare",
    "product_search_to_image_generation",
    "product_search_to_render",
}
SENSITIVE_PATTERNS = (
    re.compile(r"sk-[a-z0-9_-]+", re.IGNORECASE),
    re.compile(r"bearer\s+[a-z0-9._-]+", re.IGNORECASE),
    re.compile(r"authorization", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"data:image/[^;]+;base64,", re.IGNORECASE),
)


def test_demo_scenario_file_is_parseable() -> None:
    scenarios = _load_scenarios()

    assert isinstance(scenarios, list)
    assert len(scenarios) >= 8


def test_demo_scenario_ids_are_unique_and_required_cases_exist() -> None:
    scenarios = _load_scenarios()
    scenario_ids = [scenario["scenario_id"] for scenario in scenarios]

    assert len(scenario_ids) == len(set(scenario_ids))
    assert REQUIRED_SCENARIOS.issubset(set(scenario_ids))


def test_demo_scenarios_have_required_fields_and_expected_tools() -> None:
    for scenario in _load_scenarios():
        assert scenario["scenario_id"]
        assert scenario["title"]
        assert scenario["user_query"]
        assert isinstance(scenario["metadata"], dict)
        assert "expected_tools" in scenario
        assert isinstance(scenario["expected_tools"], list)
        assert "expected_response_contains" in scenario
        assert isinstance(scenario["expected_response_contains"], list)


def test_demo_scenarios_do_not_contain_sensitive_or_real_media_paths() -> None:
    raw = SCENARIO_PATH.read_text(encoding="utf-8")

    for pattern in SENSITIVE_PATTERNS:
        assert pattern.search(raw) is None
    assert "/home/" not in raw
    assert "C:\\" not in raw
    assert ".env" not in raw


def test_demo_scenario_media_is_mocked_by_metadata() -> None:
    for scenario in _load_scenarios():
        metadata = scenario["metadata"]
        has_media = bool(metadata.get("image_ids") or metadata.get("video_ids"))
        if has_media:
            assert metadata.get("mock_media") is True
            assert not any(_looks_like_real_media_path(value) for value in _flatten(metadata))


def _load_scenarios() -> list[dict[str, Any]]:
    return json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))


def _flatten(value: Any) -> list[str]:
    if isinstance(value, dict):
        flattened: list[str] = []
        for item in value.values():
            flattened.extend(_flatten(item))
        return flattened
    if isinstance(value, list):
        flattened = []
        for item in value:
            flattened.extend(_flatten(item))
        return flattened
    if isinstance(value, str):
        return [value]
    return []


def _looks_like_real_media_path(value: str) -> bool:
    lowered = value.lower()
    return lowered.endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".avi", ".glb", ".obj"))

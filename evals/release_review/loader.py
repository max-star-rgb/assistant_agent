from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from .contracts import ReleaseScenario


def _one_line(error: Exception) -> str:
    return " ".join(str(error).splitlines())


def load_scenario(path: Path) -> ReleaseScenario:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("scenario document must be a mapping")
        return ReleaseScenario.model_validate(payload)
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise ValueError(f"{path.name}: {_one_line(exc)}") from exc


def load_scenarios(root: Path) -> tuple[ReleaseScenario, ...]:
    paths = sorted({*root.rglob("*.yaml"), *root.rglob("*.yml")})
    scenarios: list[ReleaseScenario] = []
    origins: dict[str, Path] = {}
    for path in paths:
        scenario = load_scenario(path)
        previous = origins.get(scenario.id)
        if previous is not None:
            raise ValueError(
                f"duplicate scenario id {scenario.id!r}: {previous.name}, {path.name}"
            )
        origins[scenario.id] = path
        scenarios.append(scenario)
    return tuple(scenarios)


def scenario_hash(scenario: ReleaseScenario) -> str:
    canonical = json.dumps(
        scenario.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


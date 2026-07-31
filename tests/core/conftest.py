from __future__ import annotations

import os
from pathlib import Path

import pytest

from policy import parse_invariant_registry


CORE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = CORE_ROOT.parent.parent
INVARIANTS_PATH = CORE_ROOT / "INVARIANTS.md"
PROVIDER_MODE_ENV = "MULTIMODAL_AGENT_PROVIDER_MODE"


def registered_invariants() -> dict[str, set[str]]:
    return parse_invariant_registry(INVARIANTS_PATH)


def registered_invariant_ids() -> set[str]:
    return set(registered_invariants())


def pytest_configure(config) -> None:
    os.environ[PROVIDER_MODE_ENV] = "mock"


def pytest_collection_modifyitems(config, items) -> None:
    registered = registered_invariants()
    for item in items:
        item_path = Path(str(item.path)).resolve()
        if CORE_ROOT not in item_path.parents:
            continue
        marker = item.get_closest_marker("core_invariant")
        if marker is None or len(marker.args) != 1:
            raise pytest.UsageError(f"{item.nodeid}: missing core_invariant marker")
        invariant_id = str(marker.args[0])
        if invariant_id not in registered:
            raise pytest.UsageError(
                f"{item.nodeid}: unknown core invariant {invariant_id}"
            )
        relative_path = item_path.relative_to(REPO_ROOT).as_posix()
        if relative_path not in registered[invariant_id]:
            raise pytest.UsageError(
                f"{item.nodeid}: core invariant {invariant_id} "
                f"is not registered for {relative_path}"
            )

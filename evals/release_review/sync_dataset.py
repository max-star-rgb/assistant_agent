from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

try:
    from langfuse.api import DatasetStatus
except ImportError:  # pragma: no cover - eval extra is required for real sync
    DatasetStatus = None  # type: ignore[assignment,misc]

from .contracts import ReleaseScenario
from .loader import scenario_hash


RELEASE_REVIEW_DATASET = "assistant-agent-release-review"
GIT_ITEM_OWNER = "assistant_agent_release_review"


@dataclass(frozen=True)
class DatasetSyncResult:
    dataset_name: str
    active_item_ids: tuple[str, ...]
    archived_item_ids: tuple[str, ...]


def sync_release_dataset(
    client: Any,
    scenarios: Collection[ReleaseScenario],
    git_commit: str,
) -> DatasetSyncResult:
    client.create_dataset(
        name=RELEASE_REVIEW_DATASET,
        description=(
            "Git-owned pre-release Agent decision and staging scenarios; "
            "Langfuse stores the native Experiment, Trace and canonical Scores."
        ),
        metadata={"owner": GIT_ITEM_OWNER, "kind": "release_review"},
    )
    existing = list(client.get_dataset(RELEASE_REVIEW_DATASET).items)
    active_ids: list[str] = []
    for scenario in sorted(scenarios, key=lambda item: item.id):
        digest = scenario_hash(scenario)
        for repetition in range(1, scenario.repetitions + 1):
            item_id = _item_id(scenario.id, repetition)
            client.create_dataset_item(
                dataset_name=RELEASE_REVIEW_DATASET,
                id=item_id,
                input={"scenario_id": scenario.id, "request": scenario.request},
                expected_output={
                    "tool_contract": scenario.tool_contract.model_dump(
                        mode="json", exclude_none=True
                    ),
                    "state_assertions": [
                        assertion.model_dump(mode="json", exclude_none=True)
                        for assertion in scenario.state_assertions
                    ],
                },
                metadata={
                    "owner": GIT_ITEM_OWNER,
                    "scenario_id": scenario.id,
                    "phase": scenario.phase,
                    "capability": scenario.capability,
                    "risk": scenario.risk,
                    "scenario_hash": digest,
                    "git_commit": git_commit,
                    "repetition": repetition,
                },
                status=_dataset_status("ACTIVE"),
            )
            active_ids.append(item_id)

    archived_ids: list[str] = []
    active_set = set(active_ids)
    for item in existing:
        item_id = _item_field(item, "id")
        metadata = _item_field(item, "metadata")
        if (
            not isinstance(item_id, str)
            or item_id in active_set
            or not isinstance(metadata, dict)
            or metadata.get("owner") != GIT_ITEM_OWNER
        ):
            continue
        client.create_dataset_item(
            dataset_name=RELEASE_REVIEW_DATASET,
            id=item_id,
            input=_item_field(item, "input"),
            expected_output=_item_field(item, "expected_output"),
            metadata=metadata,
            status=_dataset_status("ARCHIVED"),
        )
        archived_ids.append(item_id)
    return DatasetSyncResult(
        dataset_name=RELEASE_REVIEW_DATASET,
        active_item_ids=tuple(active_ids),
        archived_item_ids=tuple(sorted(archived_ids)),
    )


def _item_id(scenario_id: str, repetition: int) -> str:
    return f"{RELEASE_REVIEW_DATASET}__{scenario_id}__r{repetition}"


def _dataset_status(value: str) -> Any:
    if DatasetStatus is None:
        return value
    return DatasetStatus(value)


def _item_field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


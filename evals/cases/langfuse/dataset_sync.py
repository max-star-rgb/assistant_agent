"""Local seed synchronization and Dataset item selection helpers."""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

from evals.cases.langfuse.contracts import (
    DatasetCaseCollection,
    DatasetSeed,
    DatasetSeedComposition,
    DatasetSeedResult,
)

MANAGED_BY = "assistant_agent_seed_sync_v1"


def load_dataset_seed(path: Path | str) -> DatasetSeed:
    seed_path = Path(path)
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version")
        != "assistant_agent_eval_dataset_composition_v1"
    ):
        return DatasetSeed.model_validate(payload)

    composition = DatasetSeedComposition.model_validate(payload)
    items = []
    seen_case_ids: set[str] = set()
    for relative_source in composition.case_sources:
        source_path = (seed_path.parent / relative_source).resolve()
        collection = DatasetCaseCollection.model_validate_json(
            source_path.read_text(encoding="utf-8")
        )
        for item in collection.items:
            if item.id in seen_case_ids:
                raise ValueError(
                    f"Duplicate case_id in Dataset composition: {item.id}."
                )
            seen_case_ids.add(item.id)
            items.append(item)
    return DatasetSeed(
        dataset_name=composition.dataset_name,
        description=composition.description,
        metadata=composition.metadata,
        items=items,
    )


def sync_langfuse_dataset(
    client: Any,
    seed: DatasetSeed,
) -> DatasetSeedResult:
    """Synchronize locally managed items without deleting UI-authored items.

    Langfuse requires Dataset item IDs to be unique across the whole project,
    not merely within one Dataset. Stable case IDs therefore live in metadata,
    while the native item IDs are namespaced by the target Dataset.
    """

    seed_hash = seed.content_hash()
    client.create_dataset(
        name=seed.dataset_name,
        description=seed.description,
        metadata={**seed.metadata, "seed_hash": seed_hash},
    )
    expected_item_ids = {
        managed_dataset_item_id(seed.dataset_name, item.id)
        for item in seed.items
    }
    dataset = client.get_dataset(seed.dataset_name)
    obsolete_item_ids = [
        str(item.id)
        for item in getattr(dataset, "items", [])
        if str(item.id) not in expected_item_ids
        and _is_seed_managed_item(item)
    ]
    dataset_items_api = getattr(
        getattr(client, "api", None), "dataset_items", None
    )
    if obsolete_item_ids and dataset_items_api is None:
        raise RuntimeError("Langfuse client cannot delete obsolete managed items.")
    for item_id in obsolete_item_ids:
        dataset_items_api.delete(item_id)
    for item in seed.items:
        dataset_item_id = managed_dataset_item_id(seed.dataset_name, item.id)
        client.create_dataset_item(
            dataset_name=seed.dataset_name,
            id=dataset_item_id,
            input=item.input,
            expected_output=item.expected_output,
            metadata={
                **item.metadata,
                "case_id": item.id,
                "seed_hash": seed_hash,
                "managed_by": MANAGED_BY,
            },
        )
    return DatasetSeedResult(
        dataset_name=seed.dataset_name,
        seed_hash=seed_hash,
        item_ids=[
            managed_dataset_item_id(seed.dataset_name, item.id)
            for item in seed.items
        ],
        removed_item_ids=obsolete_item_ids,
    )


def managed_dataset_item_id(dataset_name: str, case_id: str) -> str:
    """Return a readable project-unique Langfuse Dataset item ID."""

    return f"{dataset_name}__{case_id}"


def _is_seed_managed_item(item: Any) -> bool:
    metadata = getattr(item, "metadata", None)
    if not isinstance(metadata, dict) or not metadata.get("seed_hash"):
        return False
    if metadata.get("managed_by") == MANAGED_BY:
        return True
    # Compatibility with items created by the pre-namespace synchronizer.
    return metadata.get("case_id") == str(getattr(item, "id", ""))


def failed_dataset_item_ids(
    client: Any,
    *,
    dataset_name: str,
    run_name: str,
    score_names: Collection[str],
) -> list[str]:
    dataset_run = client.get_dataset_run(
        dataset_name=dataset_name,
        run_name=run_name,
    )
    trace_to_item_id = {
        str(run_item.trace_id): str(run_item.dataset_item_id)
        for run_item in dataset_run.dataset_run_items
    }
    latest_scores: dict[tuple[str, str], Any] = {}
    for trace_id in trace_to_item_id:
        page = 1
        while True:
            response = client.api.scores.get_many(
                trace_id=trace_id,
                page=page,
                limit=100,
            )
            for score in response.data:
                score_name = getattr(score, "name", None)
                if score_name not in score_names:
                    continue
                key = (trace_id, str(score_name))
                previous = latest_scores.get(key)
                if previous is None or score.timestamp > previous.timestamp:
                    latest_scores[key] = score
            if page >= response.meta.total_pages:
                break
            page += 1

    failed_trace_ids = {
        trace_id
        for (trace_id, _), score in latest_scores.items()
        if getattr(score, "data_type", None) == "BOOLEAN"
        and float(score.value) == 0.0
    }
    return [
        trace_to_item_id[str(run_item.trace_id)]
        for run_item in dataset_run.dataset_run_items
        if str(run_item.trace_id) in failed_trace_ids
    ]


def partition_available_dataset_item_ids(
    dataset: Any,
    requested_item_ids: Collection[str],
) -> tuple[list[str], list[str]]:
    available_item_ids = {str(item.id) for item in dataset.items}
    selected = [
        item_id for item_id in requested_item_ids if item_id in available_item_ids
    ]
    unavailable = [
        item_id
        for item_id in requested_item_ids
        if item_id not in available_item_ids
    ]
    return selected, unavailable

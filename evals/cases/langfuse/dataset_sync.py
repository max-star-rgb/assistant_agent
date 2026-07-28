"""Local seed synchronization and Dataset item selection helpers."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

from evals.cases.langfuse.contracts import (
    DatasetSeed,
    DatasetSeedResult,
)


def load_dataset_seed(path: Path | str) -> DatasetSeed:
    return DatasetSeed.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def sync_langfuse_dataset(
    client: Any,
    seed: DatasetSeed,
) -> DatasetSeedResult:
    """Synchronize locally managed items without deleting UI-authored items."""

    seed_hash = seed.content_hash()
    client.create_dataset(
        name=seed.dataset_name,
        description=seed.description,
        metadata={**seed.metadata, "seed_hash": seed_hash},
    )
    expected_item_ids = {item.id for item in seed.items}
    dataset = client.get_dataset(seed.dataset_name)
    obsolete_item_ids = [
        str(item.id)
        for item in getattr(dataset, "items", [])
        if str(item.id) not in expected_item_ids
        and isinstance(getattr(item, "metadata", None), dict)
        and getattr(item, "metadata").get("seed_hash")
        and getattr(item, "metadata").get("case_id") == str(item.id)
    ]
    dataset_items_api = getattr(
        getattr(client, "api", None), "dataset_items", None
    )
    if obsolete_item_ids and dataset_items_api is None:
        raise RuntimeError("Langfuse client cannot delete obsolete managed items.")
    for item_id in obsolete_item_ids:
        dataset_items_api.delete(item_id)
    for item in seed.items:
        client.create_dataset_item(
            dataset_name=seed.dataset_name,
            id=item.id,
            input=item.input,
            expected_output=item.expected_output,
            metadata={
                **item.metadata,
                "case_id": item.id,
                "seed_hash": seed_hash,
            },
        )
    return DatasetSeedResult(
        dataset_name=seed.dataset_name,
        seed_hash=seed_hash,
        item_ids=[item.id for item in seed.items],
        removed_item_ids=obsolete_item_ids,
    )


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

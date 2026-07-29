#!/usr/bin/env python3
"""Sync local Agent eval tasks to the Langfuse Dataset.

Run this file directly after editing tasks under evals/agent/tasks. It publishes
all local Git-owned tasks and removes stale Git-owned Dataset items whose local
task directories no longer exist. It never starts an Experiment.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from assistant_agent.runtime.assistant_run_service import load_env_file
from evals.agent.cli import _langfuse_client
from evals.agent.langfuse_backend import DEFAULT_DATASET_NAME, publish_tasks
from evals.agent.loader import list_task_ids, load_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish all local Agent eval tasks to Langfuse and delete stale "
            "Git-owned Dataset items."
        )
    )
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument(
        "--keep-stale",
        action="store_true",
        help="Publish local tasks but do not delete stale Git-owned Dataset items.",
    )
    args = parser.parse_args(argv)

    if not args.no_env_file:
        load_env_file(args.env_file)

    client = _langfuse_client()
    local_task_ids = list_task_ids()
    tasks = [load_task(task_id) for task_id in local_task_ids]
    published_item_ids = publish_tasks(
        client,
        tasks,
        dataset_name=args.dataset_name,
    )
    deleted_item_ids = []
    if not args.keep_stale:
        deleted_item_ids = delete_stale_git_owned_items(
            client,
            dataset_name=args.dataset_name,
            local_task_ids=set(local_task_ids),
        )
    client.flush()
    print(
        json.dumps(
            {
                "action": "sync_langfuse_dataset",
                "dataset_name": args.dataset_name,
                "published_count": len(published_item_ids),
                "published_item_ids": published_item_ids,
                "deleted_stale_count": len(deleted_item_ids),
                "deleted_stale_item_ids": deleted_item_ids,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def delete_stale_git_owned_items(
    client: Any,
    *,
    dataset_name: str,
    local_task_ids: set[str],
) -> list[str]:
    dataset = client.get_dataset(dataset_name)
    stale_item_ids = [
        item_id
        for item in dataset.items
        for item_id, task_id in [_git_owned_item_identity(item, dataset_name)]
        if item_id is not None and task_id not in local_task_ids
    ]
    for item_id in stale_item_ids:
        client.api.dataset_items.delete(id=item_id)
    return stale_item_ids


def _git_owned_item_identity(
    item: Any,
    dataset_name: str,
) -> tuple[str | None, str | None]:
    item_id = _item_field(item, "id")
    if not isinstance(item_id, str) or not item_id.startswith(f"{dataset_name}__"):
        return None, None
    task_id = None
    metadata = _item_field(item, "metadata")
    item_input = _item_field(item, "input")
    if isinstance(metadata, dict) and isinstance(metadata.get("task_id"), str):
        task_id = metadata["task_id"]
    elif isinstance(item_input, dict) and isinstance(item_input.get("task_id"), str):
        task_id = item_input["task_id"]
    if task_id is None:
        task_id = item_id.removeprefix(f"{dataset_name}__")
    return item_id, task_id


def _item_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


if __name__ == "__main__":
    raise SystemExit(main())

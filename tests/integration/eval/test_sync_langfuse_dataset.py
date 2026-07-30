"""Offline contracts for the task Dataset sync helper."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from evals.agent.tasks.sync_langfuse_dataset import delete_stale_git_owned_items


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.dataset_items: list[Any] = []
        self.deleted_item_ids: list[str] = []
        self.api = SimpleNamespace(
            dataset_items=SimpleNamespace(delete=self._delete_dataset_item)
        )

    def get_dataset(self, name: str) -> Any:
        return SimpleNamespace(name=name, items=self.dataset_items)

    def _delete_dataset_item(self, *, id: str) -> object:
        self.deleted_item_ids.append(id)
        return object()


def test_deletes_only_stale_git_owned_dataset_items() -> None:
    client = _FakeLangfuseClient()
    client.dataset_items = [
        {
            "id": "assistant-agent-regression__email_empty_result_honesty",
            "input": {"task_id": "email_empty_result_honesty"},
            "metadata": {"task_id": "email_empty_result_honesty"},
        },
        {
            "id": "assistant-agent-regression__removed_task",
            "input": {"task_id": "removed_task"},
            "metadata": {"task_id": "removed_task"},
        },
        {
            "id": "manual-dataset-item",
            "input": {"task_id": "manual_task"},
            "metadata": {"task_id": "manual_task"},
        },
    ]

    deleted = delete_stale_git_owned_items(
        client,
        dataset_name="assistant-agent-regression",
        local_task_ids={"email_empty_result_honesty"},
    )

    assert deleted == ["assistant-agent-regression__removed_task"]
    assert client.deleted_item_ids == deleted

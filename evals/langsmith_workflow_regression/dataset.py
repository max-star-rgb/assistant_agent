"""Git-owned Workflow Regression Examples and idempotent LangSmith sync."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Collection
from uuid import NAMESPACE_URL, uuid5

from langsmith.utils import LangSmithNotFoundError

from .contracts import (
    WORKFLOW_REGRESSION_DATASET,
    WorkflowDatasetExample,
)


GIT_WORKFLOW_OWNER = "git:assistant_agent"
DEFAULT_EXAMPLES_PATH = Path(__file__).with_name("examples.json")


@dataclass(frozen=True)
class WorkflowDatasetSyncResult:
    dataset_id: str
    active_example_ids: tuple[str, ...]
    archived_example_ids: tuple[str, ...]


def load_git_workflow_examples(
    path: Path = DEFAULT_EXAMPLES_PATH,
) -> tuple[WorkflowDatasetExample, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("Workflow Git Examples must be a JSON array")
    examples = tuple(WorkflowDatasetExample.model_validate(item) for item in raw)
    case_types = [item.inputs.case_type for item in examples]
    if len(examples) != 4 or len(case_types) != len(set(case_types)):
        raise RuntimeError("Workflow Git Examples must define four unique cases")
    return examples


def sync_workflow_examples(
    client: Any,
    examples: Collection[WorkflowDatasetExample],
    *,
    git_commit: str,
) -> WorkflowDatasetSyncResult:
    try:
        dataset = client.read_dataset(dataset_name=WORKFLOW_REGRESSION_DATASET)
    except LangSmithNotFoundError:
        dataset = client.create_dataset(
            WORKFLOW_REGRESSION_DATASET,
            description="Git-owned native Durable Workflow regression contracts.",
            metadata={"owner": GIT_WORKFLOW_OWNER, "kind": "workflow_regression"},
        )
    dataset_id = _id(dataset)
    existing = list(client.list_examples(dataset_id=getattr(dataset, "id")))
    owned = {
        str(getattr(item, "metadata", {}).get("case_id")): item
        for item in existing
        if getattr(item, "metadata", {}).get("owner") == GIT_WORKFLOW_OWNER
    }
    expected: set[str] = set()
    active: list[str] = []
    for example in sorted(examples, key=lambda item: item.id):
        case_id = example.id
        expected.add(case_id)
        metadata = {
            **example.metadata.model_dump(mode="json", exclude_none=True),
            "owner": GIT_WORKFLOW_OWNER,
            "case_id": case_id,
            "git_commit": git_commit,
        }
        payload = {
            "dataset_id": getattr(dataset, "id"),
            "inputs": example.inputs.model_dump(mode="json"),
            "outputs": example.outputs.model_dump(mode="json"),
            "metadata": metadata,
        }
        current = owned.get(case_id)
        if current is None:
            stable_id = uuid5(NAMESPACE_URL, f"assistant-agent-workflow:{case_id}")
            created = client.create_example(example_id=stable_id, **payload)
            active.append(_id(created))
        else:
            current_id = _id(current)
            client.update_example(current_id, **payload)
            active.append(current_id)
    archived: list[str] = []
    for case_id, item in owned.items():
        if case_id in expected:
            continue
        item_id = _id(item)
        metadata = dict(getattr(item, "metadata", {}) or {})
        metadata["active"] = False
        client.update_example(item_id, metadata=metadata)
        archived.append(item_id)
    return WorkflowDatasetSyncResult(
        dataset_id=dataset_id,
        active_example_ids=tuple(active),
        archived_example_ids=tuple(sorted(archived)),
    )


def _id(value: Any) -> str:
    result = str(getattr(value, "id", "") or "")
    if not result:
        raise RuntimeError("LangSmith Workflow Dataset object has no id")
    return result


__all__ = [
    "WorkflowDatasetSyncResult",
    "load_git_workflow_examples",
    "sync_workflow_examples",
]

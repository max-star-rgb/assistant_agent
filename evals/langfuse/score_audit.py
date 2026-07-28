"""Wait for and audit the four native Langfuse Scores of a Dataset run."""

from __future__ import annotations

import time
from collections.abc import Collection
from typing import Any, Literal

from pydantic import BaseModel, Field


class DatasetRunItemScoreAudit(BaseModel):
    dataset_item_id: str
    trace_id: str
    scores: dict[str, bool]
    missing_score_names: list[str] = Field(default_factory=list)
    failed_score_names: list[str] = Field(default_factory=list)


class DatasetRunScoreAudit(BaseModel):
    status: Literal["complete", "incomplete"]
    agent_outcome: Literal["passed", "failed", "unknown"]
    dataset_name: str
    run_name: str
    dataset_run_id: str
    required_score_names: list[str]
    items: list[DatasetRunItemScoreAudit]


def audit_dataset_run_scores(
    client: Any,
    *,
    dataset_name: str,
    run_name: str,
    required_score_names: Collection[str],
) -> DatasetRunScoreAudit:
    required = list(dict.fromkeys(required_score_names))
    dataset_run = client.get_dataset_run(
        dataset_name=dataset_name,
        run_name=run_name,
    )
    trace_to_item_id = {
        str(item.trace_id): str(item.dataset_item_id)
        for item in dataset_run.dataset_run_items
    }
    scores_by_trace: dict[str, dict[str, bool]] = {
        trace_id: {} for trace_id in trace_to_item_id
    }
    cursor = None
    while trace_to_item_id:
        response = client.api.scores_v3.get_many_v3(
            trace_id=",".join(trace_to_item_id),
            name=",".join(required),
            fields="subject",
            limit=100,
            cursor=cursor,
        )
        for score in response.data:
            trace_id = _score_trace_id(score)
            if (
                trace_id in scores_by_trace
                and score.name in required
                and score.data_type == "BOOLEAN"
            ):
                scores_by_trace[trace_id][str(score.name)] = bool(score.value)
        cursor = response.meta.cursor
        if not cursor:
            break

    items = []
    for trace_id, dataset_item_id in trace_to_item_id.items():
        item_scores = scores_by_trace[trace_id]
        items.append(
            DatasetRunItemScoreAudit(
                dataset_item_id=dataset_item_id,
                trace_id=trace_id,
                scores=item_scores,
                missing_score_names=[
                    name for name in required if name not in item_scores
                ],
                failed_score_names=[
                    name
                    for name in required
                    if item_scores.get(name) is False
                ],
            )
        )
    complete = bool(items) and all(
        not item.missing_score_names for item in items
    )
    return DatasetRunScoreAudit(
        status="complete" if complete else "incomplete",
        agent_outcome=(
            "unknown"
            if not complete
            else "failed"
            if any(item.failed_score_names for item in items)
            else "passed"
        ),
        dataset_name=dataset_name,
        run_name=run_name,
        dataset_run_id=str(dataset_run.id),
        required_score_names=required,
        items=items,
    )


def wait_for_dataset_run_scores(
    client: Any,
    *,
    dataset_name: str,
    run_name: str,
    required_score_names: Collection[str],
    timeout_seconds: float,
    poll_interval_seconds: float = 2.0,
) -> DatasetRunScoreAudit:
    deadline = time.monotonic() + timeout_seconds
    while True:
        audit = audit_dataset_run_scores(
            client,
            dataset_name=dataset_name,
            run_name=run_name,
            required_score_names=required_score_names,
        )
        if audit.status == "complete" or time.monotonic() >= deadline:
            return audit
        time.sleep(poll_interval_seconds)


def _score_trace_id(score: Any) -> str | None:
    subject = getattr(score, "subject", None)
    if subject is None:
        return None
    if getattr(subject, "kind", None) == "trace":
        return str(subject.id)
    if getattr(subject, "kind", None) == "observation":
        trace_id = getattr(subject, "trace_id", None)
        return str(trace_id) if trace_id else None
    return None

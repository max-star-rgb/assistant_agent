from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET

try:
    from langfuse.api import DatasetStatus
except ImportError:  # pragma: no cover - real Langfuse SDK provides this type
    DatasetStatus = None  # type: ignore[assignment,misc]


RUNTIME_REGRESSION_OWNER = "assistant_agent_runtime_regression"
QUALITY_SCORE_PREFIX = "assistant_agent.quality."
_SAFE_REVIEWER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RuntimeRegressionPromotionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: str
    dataset_item_id: str
    source_trace_id: str
    source_observation_id: str
    failed_score_names: tuple[str, ...]


def promote_failed_score(
    client: Any,
    *,
    score_id: str,
    reviewed_by: str,
    promoted_at: datetime | None = None,
) -> RuntimeRegressionPromotionResult:
    """Promote one reviewed, persisted failing observation Score into Langfuse."""

    if not score_id:
        raise ValueError("score_id is required")
    if not _SAFE_REVIEWER.fullmatch(reviewed_by):
        raise ValueError("reviewed_by must be a safe identifier")
    selected = _selected_score(client, score_id)
    _require_actionable_score(selected)
    subject = _field(selected, "subject")
    trace_id = _field(subject, "traceId")
    observation_id = _field(subject, "id")
    if not isinstance(trace_id, str) or not trace_id:
        raise ValueError("failing Score subject is missing traceId")
    if not isinstance(observation_id, str) or not observation_id:
        raise ValueError("failing Score subject is missing observation id")

    observations = _observations_for_trace(client, trace_id)
    root = _runtime_root(observations)
    request = _request_text(root)
    failed_score_names = _failed_score_names(client, trace_id)
    if _field(selected, "name") not in failed_score_names:
        raise ValueError("selected failing Score is not present on its source trace")

    dataset_item_id = _dataset_item_id(trace_id)
    timestamp = promoted_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("promoted_at must be timezone-aware")
    client.create_dataset(
        name=RUNTIME_REGRESSION_DATASET,
        description=(
            "Human-reviewed failures promoted directly from daily Langfuse "
            "observations for production-runtime regression experiments."
        ),
        metadata={"owner": RUNTIME_REGRESSION_OWNER, "kind": "runtime_regression"},
    )
    client.create_dataset_item(
        dataset_name=RUNTIME_REGRESSION_DATASET,
        id=dataset_item_id,
        input={"request": request},
        expected_output={
            "required_scores": {name: True for name in failed_score_names}
        },
        metadata={
            "owner": RUNTIME_REGRESSION_OWNER,
            "source": "langfuse_live_score",
            "source_score_id": score_id,
            "failed_score_names": list(failed_score_names),
            "root_observation_id": _field(root, "id"),
            "reviewed_by": reviewed_by,
            "promoted_at": timestamp.isoformat(),
        },
        source_trace_id=trace_id,
        source_observation_id=observation_id,
        status=_dataset_status("ACTIVE"),
    )
    return RuntimeRegressionPromotionResult(
        dataset_name=RUNTIME_REGRESSION_DATASET,
        dataset_item_id=dataset_item_id,
        source_trace_id=trace_id,
        source_observation_id=observation_id,
        failed_score_names=failed_score_names,
    )


def _selected_score(client: Any, score_id: str) -> Any:
    response = client.api.scores_v3.get_many_v3(
        id=score_id,
        fields="core,subject",
        limit=2,
    )
    matches = list(_field(response, "data") or ())
    if len(matches) != 1:
        raise ValueError(f"expected exactly one Langfuse Score for id {score_id!r}")
    return matches[0]


def _require_actionable_score(score: Any) -> None:
    name = _field(score, "name")
    if not isinstance(name, str) or not name.startswith(QUALITY_SCORE_PREFIX):
        raise ValueError("only a canonical quality Score can be promoted")
    if _field(score, "source") != "EVAL":
        raise ValueError("quality Score source must be EVAL")
    if _field(score, "dataType") != "BOOLEAN" or _field(score, "value") is not False:
        raise ValueError("quality Score must be false BOOLEAN")
    subject = _field(score, "subject")
    if _field(subject, "kind") != "observation":
        raise ValueError("quality Score subject must be an observation")


def _failed_score_names(client: Any, trace_id: str) -> tuple[str, ...]:
    failed: set[str] = set()
    cursor = None
    while True:
        response = client.api.scores_v3.get_many_v3(
            trace_id=trace_id,
            fields="core,subject",
            limit=100,
            cursor=cursor,
        )
        for score in _field(response, "data") or ():
            name = _field(score, "name")
            subject = _field(score, "subject")
            if (
                isinstance(name, str)
                and name.startswith(QUALITY_SCORE_PREFIX)
                and _field(score, "source") == "EVAL"
                and _field(score, "dataType") == "BOOLEAN"
                and _field(score, "value") is False
                and _field(subject, "kind") == "observation"
            ):
                failed.add(name)
        cursor = _field(_field(response, "meta"), "cursor")
        if not cursor:
            return tuple(sorted(failed))


def _observations_for_trace(client: Any, trace_id: str) -> list[Any]:
    observations: list[Any] = []
    cursor = None
    while True:
        response = client.api.observations.get_many(
            trace_id=trace_id,
            fields="core,basic,io",
            limit=100,
            cursor=cursor,
        )
        observations.extend(_field(response, "data") or ())
        cursor = _field(_field(response, "meta"), "cursor")
        if not cursor:
            return observations


def _runtime_root(observations: list[Any]) -> Any:
    roots = [
        item
        for item in observations
        if _field(item, "name") == "agent.runtime"
        and _field(item, "type") == "SPAN"
        and _field(item, "parentObservationId") is None
    ]
    if len(roots) != 1:
        raise ValueError("source trace must contain exactly one root agent.runtime observation")
    return roots[0]


def _request_text(root: Any) -> str:
    payload = _json_value(_field(root, "input"))
    if not isinstance(payload, dict):
        raise ValueError("root agent.runtime input is unavailable")
    if payload.get("truncated") is True:
        raise ValueError("root agent.runtime input is truncated")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("root agent.runtime input has no user content")
    return content


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _dataset_item_id(trace_id: str) -> str:
    digest = hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:24]
    return f"{RUNTIME_REGRESSION_DATASET}__{digest}"


def _dataset_status(value: str) -> Any:
    if DatasetStatus is None:
        return value
    return DatasetStatus(value)


def _field(value: Any, name: str) -> Any:
    snake_name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    if isinstance(value, dict):
        return value.get(name, value.get(snake_name))
    return getattr(value, name, getattr(value, snake_name, None))

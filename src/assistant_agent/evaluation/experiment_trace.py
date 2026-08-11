"""Fail-closed Langfuse Experiment trace hierarchy validation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
import math
import time
from typing import Any


def wait_for_experiment_trace_completeness(
    client: Any,
    *,
    experiment_id: str,
    experiment_item_ids: Sequence[str],
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Require the SDK task and canonical Runtime hierarchy for every item."""

    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("Trace wait timeout and poll interval must be positive")
    expected = set(experiment_item_ids)
    attempts = math.floor(timeout_seconds / poll_interval_seconds) + 1
    from_start_time = datetime.now(timezone.utc) - timedelta(days=1)
    problems: dict[str, str] = {}
    for attempt in range(attempts):
        experiment_items = _load_experiment_items(
            client,
            experiment_id=experiment_id,
            from_start_time=from_start_time,
        )
        by_item: dict[str, list[Any]] = {}
        for item in experiment_items:
            item_id = _field(item, "experiment_item_id")
            if item_id in expected:
                by_item.setdefault(str(item_id), []).append(item)
        resolved: dict[str, str] = {}
        problems = {}
        for item_id in sorted(expected):
            matches = by_item.get(item_id, [])
            if len(matches) != 1:
                problems[item_id] = f"experiment items={len(matches)}"
                continue
            trace_id = _field(matches[0], "trace_id")
            if not isinstance(trace_id, str) or not trace_id:
                problems[item_id] = "trace id missing"
                continue
            observations = load_experiment_trace_observations(client, trace_id)
            problem = experiment_trace_hierarchy_problem(observations)
            if problem is None:
                resolved[item_id] = trace_id
            else:
                problems[item_id] = problem
        if not problems:
            return resolved
        if attempt + 1 < attempts:
            sleep(poll_interval_seconds)
    raise RuntimeError(
        "Langfuse Experiment trace completeness timeout; problems="
        + repr(problems)
    )


def _load_experiment_items(
    client: Any,
    *,
    experiment_id: str,
    from_start_time: datetime,
) -> list[Any]:
    items: list[Any] = []
    cursor = None
    while True:
        response = client.api.experiments.list_items(
            from_start_time=from_start_time,
            experiment_id=experiment_id,
            fields="core",
            limit=100,
            cursor=cursor,
        )
        items.extend(_field(response, "data") or ())
        cursor = _field(_field(response, "meta"), "cursor")
        if not cursor:
            return items


def experiment_trace_hierarchy_problem(observations: Sequence[Any]) -> str | None:
    """Return a stable problem when the production Runtime subtree is incomplete."""

    item_runs = _named(observations, "experiment-item-run", "SPAN")
    if len(item_runs) != 1:
        return f"experiment-item-run observations={len(item_runs)}"
    tasks = _named(observations, "experiment-item-task", "SPAN")
    if len(tasks) != 1:
        return f"experiment-item-task observations={len(tasks)}"
    item_run_id = _field(item_runs[0], "id")
    task_id = _field(tasks[0], "id")
    if _field(tasks[0], "parent_observation_id") != item_run_id:
        return "experiment-item-task parent is not experiment-item-run"
    runtimes = _named(observations, "agent.runtime", "SPAN")
    direct_runtimes = [
        item
        for item in runtimes
        if _field(item, "parent_observation_id") == task_id
    ]
    if len(direct_runtimes) != 1:
        if len(runtimes) == 1:
            return "agent.runtime parent is not experiment-item-task"
        return f"agent.runtime direct children={len(direct_runtimes)}"
    runtime_id = _field(direct_runtimes[0], "id")
    if not isinstance(runtime_id, str):
        return "agent.runtime parent is not experiment-item-task"
    llm_calls = _named(observations, "llm.chat", "GENERATION")
    if not any(_is_descendant(item, runtime_id, observations) for item in llm_calls):
        return "llm.chat descendant of agent.runtime is missing"
    return None


def experiment_task_observation_id(observations: Sequence[Any]) -> str | None:
    tasks = _named(observations, "experiment-item-task", "SPAN")
    if len(tasks) != 1:
        return None
    observation_id = _field(tasks[0], "id")
    return observation_id if isinstance(observation_id, str) else None


def load_experiment_trace_observations(client: Any, trace_id: str) -> list[Any]:
    observations: list[Any] = []
    cursor = None
    while True:
        response = client.api.observations.get_many(
            trace_id=trace_id,
            fields="core,basic",
            limit=100,
            cursor=cursor,
        )
        observations.extend(_field(response, "data") or ())
        cursor = _field(_field(response, "meta"), "cursor")
        if not cursor:
            return observations


def _named(observations: Sequence[Any], name: str, observation_type: str) -> list[Any]:
    return [
        item
        for item in observations
        if _field(item, "name") == name and _field(item, "type") == observation_type
    ]


def _is_descendant(item: Any, ancestor_id: Any, observations: Sequence[Any]) -> bool:
    by_id = {_field(candidate, "id"): candidate for candidate in observations}
    parent_id = _field(item, "parent_observation_id")
    visited: set[Any] = set()
    while parent_id is not None and parent_id not in visited:
        if parent_id == ancestor_id:
            return True
        visited.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            return False
        parent_id = _field(parent, "parent_observation_id")
    return False


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)

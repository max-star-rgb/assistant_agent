from __future__ import annotations

import time
from collections.abc import Collection
from typing import Any

from assistant_agent.evaluation.experiment_trace import (
    experiment_task_observation_id,
    experiment_trace_hierarchy_problem,
    load_experiment_trace_observations,
)

from .contracts import ReleaseScenario
from .experiment import ReleaseExperimentResult
from .report import CANONICAL_TASK_SCORES, ReleaseItemAssessment


def audit_release_scores(
    client: Any,
    result: ReleaseExperimentResult,
    scenarios: Collection[ReleaseScenario],
    *,
    attempts: int = 30,
    retry_delay_seconds: float = 0.5,
) -> tuple[ReleaseItemAssessment, ...]:
    scenario_by_id = {scenario.id: scenario for scenario in scenarios}
    item_results = {
        _result_item_key(item_result): item_result
        for item_result in result.native_result.item_results
    }
    assessments: list[ReleaseItemAssessment] = []
    for key in result.expected_item_keys:
        scenario_id, repetition_text = key.rsplit(":r", 1)
        scenario = scenario_by_id[scenario_id]
        item_result = item_results.get(key)
        if item_result is None:
            assessments.append(
                _infrastructure_assessment(
                    scenario,
                    int(repetition_text),
                    None,
                    "experiment_item_failed",
                )
            )
            continue
        trace_id = getattr(item_result, "trace_id", None)
        if not isinstance(trace_id, str) or not trace_id:
            assessments.append(
                _infrastructure_assessment(
                    scenario,
                    int(repetition_text),
                    None,
                    "trace_missing",
                )
            )
            continue
        try:
            scores = _persisted_scores(
                client,
                trace_id,
                attempts=attempts,
                retry_delay_seconds=retry_delay_seconds,
            )
        except RuntimeError as exc:
            assessments.append(
                _infrastructure_assessment(
                    scenario,
                    int(repetition_text),
                    trace_id,
                    f"score_audit_failed:{exc}",
                )
            )
            continue
        assessments.append(
            ReleaseItemAssessment(
                scenario_id=scenario.id,
                repetition=int(repetition_text),
                phase=scenario.phase,
                risk=scenario.risk,
                scenario_hash=_item_scenario_hash(item_result),
                trace_id=trace_id,
                scores=scores,
            )
        )
    return tuple(assessments)


def _persisted_scores(
    client: Any,
    trace_id: str,
    *,
    attempts: int,
    retry_delay_seconds: float,
) -> dict[str, bool]:
    detail = "not queried"
    for attempt in range(attempts):
        observations = load_experiment_trace_observations(client, trace_id)
        hierarchy_problem = experiment_trace_hierarchy_problem(observations)
        observation_id = experiment_task_observation_id(observations)
        if hierarchy_problem is None and observation_id is not None:
            response = client.api.scores_v3.get_many_v3(
                limit=100,
                fields="subject",
                name=",".join(CANONICAL_TASK_SCORES),
                trace_id=trace_id,
                observation_id=observation_id,
            )
            relevant = [score for score in response.data if score.name in CANONICAL_TASK_SCORES]
            names = [score.name for score in relevant]
            missing = sorted(set(CANONICAL_TASK_SCORES) - set(names))
            duplicates = sorted(name for name in set(names) if names.count(name) > 1)
            if not missing and not duplicates:
                resolved: dict[str, bool] = {}
                invalid: list[str] = []
                for score in relevant:
                    subject = getattr(score, "subject", None)
                    value = getattr(score, "value", None)
                    if (
                        getattr(score, "data_type", None) != "BOOLEAN"
                        or not isinstance(value, bool)
                        or getattr(subject, "kind", None) != "observation"
                        or getattr(subject, "trace_id", None) != trace_id
                        or getattr(subject, "id", None) != observation_id
                    ):
                        invalid.append(score.name)
                    else:
                        resolved[score.name] = value
                if not invalid:
                    return resolved
                detail = f"invalid={sorted(invalid)}"
            else:
                detail = f"missing={missing}, duplicates={duplicates}"
        else:
            detail = hierarchy_problem or "experiment-item-task id missing"
        if attempt + 1 < attempts and retry_delay_seconds > 0:
            time.sleep(retry_delay_seconds)
    raise RuntimeError(detail)


def _result_item_key(item_result: Any) -> str:
    item = getattr(item_result, "item", None)
    metadata = item.get("metadata") if isinstance(item, dict) else getattr(item, "metadata", None)
    if not isinstance(metadata, dict):
        return "invalid:r0"
    return f"{metadata.get('scenario_id')}:r{metadata.get('repetition')}"


def _item_scenario_hash(item_result: Any) -> str:
    item = getattr(item_result, "item", None)
    metadata = item.get("metadata") if isinstance(item, dict) else getattr(item, "metadata", None)
    return str((metadata or {}).get("scenario_hash") or "")


def _infrastructure_assessment(
    scenario: ReleaseScenario,
    repetition: int,
    trace_id: str | None,
    status: str,
) -> ReleaseItemAssessment:
    from .loader import scenario_hash

    return ReleaseItemAssessment(
        scenario_id=scenario.id,
        repetition=repetition,
        phase=scenario.phase,
        risk=scenario.risk,
        scenario_hash=scenario_hash(scenario),
        trace_id=trace_id,
        infrastructure_status=status,
    )

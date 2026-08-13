"""Deterministic evaluators over bounded native graph evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .contracts import WorkflowReferenceOutput


PLAN_ADMISSION_FEEDBACK_KEY = "assistant_agent.workflow.plan_admission"
DAG_TRAJECTORY_FEEDBACK_KEY = "assistant_agent.workflow.dag_trajectory"
CONSTRAINT_ARTIFACT_FEEDBACK_KEY = (
    "assistant_agent.workflow.constraint_artifact_quality"
)
REPAIR_RESUME_FEEDBACK_KEY = "assistant_agent.workflow.repair_resume"
REQUIRED_WORKFLOW_FEEDBACK_KEYS = (
    PLAN_ADMISSION_FEEDBACK_KEY,
    DAG_TRAJECTORY_FEEDBACK_KEY,
    CONSTRAINT_ARTIFACT_FEEDBACK_KEY,
    REPAIR_RESUME_FEEDBACK_KEY,
)


@dataclass(frozen=True)
class WorkflowEvaluationResult:
    key: str
    score: bool
    comment: str


def evaluate_plan_admission(
    actual: dict[str, Any], reference: WorkflowReferenceOutput
) -> WorkflowEvaluationResult:
    plan = _mapping(actual.get("plan"))
    score = (
        actual.get("terminal_status") == reference.terminal_status
        and tuple(plan.get("node_ids") or ()) == reference.plan.node_ids
        and {
            str(key): tuple(value)
            for key, value in _mapping(plan.get("dependencies")).items()
        }
        == reference.plan.dependencies
    )
    return _result(PLAN_ADMISSION_FEEDBACK_KEY, score, "plan contract")


def evaluate_dag_trajectory(
    actual: dict[str, Any], reference: WorkflowReferenceOutput
) -> WorkflowEvaluationResult:
    trajectory = _trajectory(actual)
    current_generation: dict[str, int] = {}
    positions: dict[tuple[str, int], int] = {}
    unique = True
    for index, item in enumerate(trajectory):
        key = (item["node_id"], item["generation"])
        if key in positions:
            unique = False
        positions[key] = index
        current_generation[item["node_id"]] = max(
            item["generation"], current_generation.get(item["node_id"], -1)
        )
    coverage = set(current_generation) == set(reference.plan.node_ids)
    dependencies_ok = all(
        positions.get((dependency, current_generation.get(dependency, -1)), 10**9)
        < positions.get((node_id, generation), -1)
        for node_id, generation in current_generation.items()
        for dependency in reference.plan.dependencies[node_id]
    )
    return _result(
        DAG_TRAJECTORY_FEEDBACK_KEY,
        bool(trajectory and unique and coverage and dependencies_ok),
        "native DAG trajectory",
    )


def evaluate_constraint_artifact_quality(
    actual: dict[str, Any], reference: WorkflowReferenceOutput
) -> WorkflowEvaluationResult:
    evidence = _mapping(actual.get("evaluation_evidence"))
    constraints = set(evidence.get("constraint_ids") or ())
    deliverables = _mapping(evidence.get("deliverable_artifact_refs"))
    artifact_refs = set(actual.get("result_artifact_refs") or ())
    deliverables_ok = all(
        isinstance(deliverables.get(name), (list, tuple))
        and bool(deliverables[name])
        and set(deliverables[name]).issubset(artifact_refs)
        for name in reference.evaluation_contract.deliverables
    )
    score = (
        constraints == set(reference.evaluation_contract.constraint_ids)
        and deliverables_ok
    )
    return _result(
        CONSTRAINT_ARTIFACT_FEEDBACK_KEY,
        score,
        "constraint and opaque artifact evidence",
    )


def evaluate_repair_resume(
    actual: dict[str, Any], reference: WorkflowReferenceOutput
) -> WorkflowEvaluationResult:
    evidence = _mapping(actual.get("evaluation_evidence"))
    expected_scope = set(reference.evaluation_contract.repair_scope)
    actual_scope = set(evidence.get("repair_scope") or ())
    trajectory = _trajectory(actual)
    rerun = {item["node_id"] for item in trajectory if item["generation"] > 0}
    score = (
        actual_scope == expected_scope
        and rerun == expected_scope
        and evidence.get("resume_equivalent")
        is reference.evaluation_contract.resume_equivalent
    )
    return _result(REPAIR_RESUME_FEEDBACK_KEY, score, "repair and resume facts")


def langsmith_evaluators() -> tuple[Callable[..., dict[str, object]], ...]:
    def adapter(function: Callable[..., WorkflowEvaluationResult]):
        def evaluator(run: Any, example: Any) -> dict[str, object]:
            outputs = _mapping(getattr(run, "outputs", None))
            reference = WorkflowReferenceOutput.model_validate(
                getattr(example, "outputs", None)
            )
            result = function(outputs, reference)
            return {"key": result.key, "score": result.score, "comment": result.comment}

        return evaluator

    return tuple(
        adapter(function)
        for function in (
            evaluate_plan_admission,
            evaluate_dag_trajectory,
            evaluate_constraint_artifact_quality,
            evaluate_repair_resume,
        )
    )


def _trajectory(actual: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in actual.get("trajectory") or ():
        item = _mapping(raw)
        node_id = item.get("node_id")
        generation = item.get("generation")
        profile = item.get("profile")
        if (
            isinstance(node_id, str)
            and isinstance(generation, int)
            and not isinstance(generation, bool)
            and generation >= 0
            and profile in {"worker", "verifier"}
        ):
            result.append(
                {"node_id": node_id, "generation": generation, "profile": profile}
            )
    return result


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _result(key: str, score: bool, comment: str) -> WorkflowEvaluationResult:
    return WorkflowEvaluationResult(key=key, score=score, comment=comment)


__all__ = [
    "REQUIRED_WORKFLOW_FEEDBACK_KEYS",
    "WorkflowEvaluationResult",
    "evaluate_constraint_artifact_quality",
    "evaluate_dag_trajectory",
    "evaluate_plan_admission",
    "evaluate_repair_resume",
    "langsmith_evaluators",
]

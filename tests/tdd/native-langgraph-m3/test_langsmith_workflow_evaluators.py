from __future__ import annotations

import pytest
from pydantic import ValidationError

from evals.langsmith_workflow_regression.contracts import (
    WorkflowDatasetExample,
    validate_active_examples,
)
from evals.langsmith_workflow_regression.evaluators import (
    evaluate_constraint_artifact_quality,
    evaluate_dag_trajectory,
    evaluate_plan_admission,
    evaluate_repair_resume,
)


def _example(**overrides) -> dict:
    value = {
        "id": "example-parallel",
        "inputs": {
            "schema_version": "workflow_experiment_input_v1",
            "case_type": "parallel_join",
            "workflow_id": "wf-eval",
            "submission": {
                "workflow_type": "deep_research",
                "objective": "produce a report",
                "deliverables": ["report"],
                "constraints": ["cite sources"],
                "inputs": {
                    "schema_version": "deep_research_inputs_v2",
                    "research_questions": ["question"],
                },
                "requested_budget": {
                    "model_calls": 8,
                    "tool_calls": 8,
                    "workflow_quanta": 8,
                    "deadline_seconds": 3600,
                },
                "durability_reasons": ["multi_stage"],
                "seed_artifact_refs": [],
                "idempotency_key": "eval-example-parallel",
            },
            "truncated": False,
        },
        "outputs": {
            "terminal_status": "completed",
            "plan": {
                "node_ids": ["research_a", "research_b", "synthesize"],
                "dependencies": {
                    "research_a": [],
                    "research_b": [],
                    "synthesize": ["research_a", "research_b"],
                },
            },
            "evaluation_contract": {
                "deliverables": {"report": "synthesize"},
                "constraint_ids": ["cite_sources"],
                "repair_scope": [],
                "resume_equivalent": True,
            },
        },
        "metadata": {
            "active": True,
            "risk": "high",
            "source_trace_id": "trace-source-safe",
        },
    }
    value.update(overrides)
    return value


def _actual(**overrides) -> dict:
    value = {
        "workflow_id": "wf-eval",
        "terminal_status": "completed",
        "plan": {
            "node_ids": ["research_a", "research_b", "synthesize"],
            "dependencies": {
                "research_a": [],
                "research_b": [],
                "synthesize": ["research_a", "research_b"],
            },
        },
        "trajectory": [
            {"node_id": "research_a", "generation": 0, "profile": "worker"},
            {"node_id": "research_b", "generation": 0, "profile": "worker"},
            {"node_id": "synthesize", "generation": 0, "profile": "worker"},
        ],
        "result_artifact_refs": [
            "artifact://research-a",
            "artifact://research-b",
            "artifact://report",
        ],
        "evaluation_evidence": {
            "constraint_ids": ["cite_sources"],
            "deliverable_artifact_refs": {"report": ["artifact://report"]},
            "repair_scope": [],
            "resume_equivalent": True,
        },
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "inputs": '{"workflow_id":"wf-eval"}'},
        lambda value: {
            **value,
            "inputs": {**value["inputs"], "truncated": True},
        },
        lambda value: {
            **value,
            "inputs": {
                **value["inputs"],
                "submission": {
                    **value["inputs"]["submission"],
                    "deliverables": [],
                },
            },
        },
        lambda value: {
            **value,
            "inputs": {
                **value["inputs"],
                "submission": {
                    **value["inputs"]["submission"],
                    "constraints": [],
                },
            },
        },
        lambda value: {
            **value,
            "metadata": {**value["metadata"], "unsafe_note": "secret"},
        },
    ],
)
def test_workflow_dataset_contract_rejects_unsafe_or_incomplete_examples(mutation):
    with pytest.raises((ValidationError, ValueError, TypeError)):
        WorkflowDatasetExample.model_validate(mutation(_example()))


def test_workflow_dataset_requires_at_least_one_active_example() -> None:
    inactive = _example(metadata={"active": False, "risk": "high"})
    with pytest.raises(RuntimeError, match="no active examples"):
        validate_active_examples([inactive])


def test_four_local_evaluators_accept_complete_native_workflow_evidence() -> None:
    example = WorkflowDatasetExample.model_validate(_example())
    actual = _actual()

    assert evaluate_plan_admission(actual, example.outputs).score is True
    assert evaluate_dag_trajectory(actual, example.outputs).score is True
    assert evaluate_constraint_artifact_quality(actual, example.outputs).score is True
    assert evaluate_repair_resume(actual, example.outputs).score is True


def test_dag_evaluator_rejects_duplicate_generation_and_dependency_inversion() -> None:
    example = WorkflowDatasetExample.model_validate(_example())
    actual = _actual(
        trajectory=[
            {"node_id": "synthesize", "generation": 0, "profile": "worker"},
            {"node_id": "research_a", "generation": 0, "profile": "worker"},
            {"node_id": "research_a", "generation": 0, "profile": "worker"},
            {"node_id": "research_b", "generation": 0, "profile": "worker"},
        ]
    )

    result = evaluate_dag_trajectory(actual, example.outputs)

    assert result.score is False
    assert result.key == "assistant_agent.workflow.dag_trajectory"


def test_repair_evaluator_rejects_rerun_of_unrelated_branch() -> None:
    raw = _example()
    raw["outputs"]["evaluation_contract"]["repair_scope"] = ["research_a"]
    example = WorkflowDatasetExample.model_validate(raw)
    actual = _actual(
        trajectory=[
            {"node_id": "research_a", "generation": 0, "profile": "worker"},
            {"node_id": "research_b", "generation": 0, "profile": "worker"},
            {"node_id": "synthesize", "generation": 0, "profile": "worker"},
            {"node_id": "research_a", "generation": 1, "profile": "worker"},
            {"node_id": "research_b", "generation": 1, "profile": "worker"},
            {"node_id": "synthesize", "generation": 1, "profile": "worker"},
        ],
        evaluation_evidence={
            **_actual()["evaluation_evidence"],
            "repair_scope": ["research_a", "research_b", "synthesize"],
        },
    )

    assert evaluate_repair_resume(actual, example.outputs).score is False

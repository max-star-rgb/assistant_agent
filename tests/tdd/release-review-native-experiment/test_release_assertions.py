from __future__ import annotations

import pytest

from evals.release_review.assertions import evaluate_task_conformance
from evals.release_review.contracts import ReleaseScenario
from evals.release_review.evidence import ReleaseRunEvidence, ReleaseToolCallEvidence


def _scenario() -> ReleaseScenario:
    return ReleaseScenario.model_validate(
        {
            "id": "assertion_probe",
            "phase": "decision",
            "capability": "probe",
            "risk": "high",
            "request": "run the tools",
            "tool_contract": {
                "required": ["research.start", "research.read"],
                "allowed": ["web.search"],
                "forbidden": ["calendar.create"],
                "arguments": [
                    {"tool": "research.start", "path": "topic", "equals": "agent"},
                    {"tool": "research.start", "path": "tags", "contains": "eval"},
                    {"tool": "research.start", "path": "depth", "gte": 2},
                    {"tool": "research.start", "path": "owner", "exists": True},
                    {"tool": "research.start", "path": "tags", "length": 2},
                ],
                "sequence": {
                    "before": [["research.start", "research.read"]],
                    "before_final_response": ["research.read"],
                },
            },
            "fixtures": {
                "research.start": [{"success": True, "data": {}}],
                "research.read": [{"success": True, "data": {}}],
            },
            "state_assertions": [
                {"path": "status", "equals": "completed"},
                {"path": "response.data.sources", "length": 2},
            ],
        }
    )


def _passing_evidence() -> ReleaseRunEvidence:
    return ReleaseRunEvidence(
        calls=(
            ReleaseToolCallEvidence(
                tool_name="research.start",
                input={
                    "topic": "agent",
                    "tags": ["eval", "tools"],
                    "depth": 3,
                    "owner": "release",
                },
                call_index=1,
                status="succeeded",
                before_final_response=True,
            ),
            ReleaseToolCallEvidence(
                tool_name="web.search",
                input={"query": "agent"},
                call_index=2,
                status="succeeded",
                before_final_response=True,
            ),
            ReleaseToolCallEvidence(
                tool_name="research.read",
                input={"id": "one"},
                call_index=3,
                status="succeeded",
                before_final_response=True,
            ),
        ),
        final_state={
            "status": "completed",
            "response": {"data": {"sources": ["a", "b"]}},
        },
    )


def test_all_contract_families_pass_in_stable_order() -> None:
    result = evaluate_task_conformance(_scenario(), _passing_evidence())

    assert result.passed is True
    assert [item.key.split(":", 1)[0] for item in result.assertions] == [
        "required",
        "required",
        "forbidden",
        "allowed",
        "allowed",
        "allowed",
        "arguments",
        "arguments",
        "arguments",
        "arguments",
        "arguments",
        "sequence",
        "sequence",
        "state",
        "state",
    ]
    assert all(item.passed for item in result.assertions)


@pytest.mark.parametrize(
    ("mutation", "expected_key"),
    [
        ("missing_required", "required:research.read"),
        ("forbidden", "forbidden:calendar.create"),
        ("outside_allowlist", "allowed:files.delete"),
        ("wrong_argument", "arguments:0:research.start:topic"),
        ("wrong_order", "sequence:before:research.start:research.read"),
        ("early_final", "sequence:before_final_response:research.read"),
        ("wrong_state", "state:0:status"),
    ],
)
def test_each_contract_failure_has_a_stable_key(
    mutation: str, expected_key: str
) -> None:
    evidence = _passing_evidence()
    calls = list(evidence.calls)
    final_state = dict(evidence.final_state)
    if mutation == "missing_required":
        calls = [call for call in calls if call.tool_name != "research.read"]
    elif mutation == "forbidden":
        calls.append(
            ReleaseToolCallEvidence(
                tool_name="calendar.create",
                input={},
                call_index=4,
                status="succeeded",
                before_final_response=True,
            )
        )
    elif mutation == "outside_allowlist":
        calls.append(
            ReleaseToolCallEvidence(
                tool_name="files.delete",
                input={},
                call_index=4,
                status="succeeded",
                before_final_response=True,
            )
        )
    elif mutation == "wrong_argument":
        calls[0] = calls[0].model_copy(update={"input": {**calls[0].input, "topic": "wrong"}})
    elif mutation == "wrong_order":
        calls[0] = calls[0].model_copy(update={"call_index": 4})
    elif mutation == "early_final":
        calls[2] = calls[2].model_copy(update={"before_final_response": False})
    elif mutation == "wrong_state":
        final_state["status"] = "failed"

    result = evaluate_task_conformance(
        _scenario(), evidence.model_copy(update={"calls": tuple(calls), "final_state": final_state})
    )

    failed_keys = {item.key for item in result.assertions if not item.passed}
    assert expected_key in failed_keys
    assert result.passed is False


def test_infrastructure_failure_is_not_converted_to_a_false_quality_score() -> None:
    evidence = _passing_evidence().model_copy(
        update={"infrastructure_error": "provider_timeout"}
    )

    result = evaluate_task_conformance(_scenario(), evidence)

    assert result.passed is None
    assert result.assertions == ()
    assert result.failure_owner == "infrastructure"

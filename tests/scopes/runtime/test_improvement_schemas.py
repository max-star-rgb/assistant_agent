from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.schemas.improvement import (
    CandidateCheck,
    CandidateEvaluation,
    ImprovementCandidate,
    ImprovementDecision,
    ImprovementEvidence,
    ImprovementOpportunity,
    ImprovementTargetRef,
)


def test_evidence_defaults_to_redacted_and_validates_confidence_inputs() -> None:
    evidence = ImprovementEvidence(
        evidence_id="ev_1",
        source_type="trajectory",
        source_ref="trace_1",
        target_hints=[ImprovementTargetRef(target_type="runtime", target_ref="assistant_loop")],
        symptom_code="assistant_loop_limit_reached",
        summary="Loop guard stopped the run.",
    )

    assert evidence.redacted is True
    assert evidence.schema_version == "improvement_evidence_v1"
    assert evidence.attributes == {}


def test_opportunity_confidence_must_be_between_zero_and_one() -> None:
    with pytest.raises(ValidationError):
        ImprovementOpportunity(
            opportunity_id="op_1",
            target_type="runtime",
            target_ref="assistant_loop",
            evidence_refs=["ev_1"],
            pattern_code="assistant_loop_limit_reached",
            problem_statement="Loop limit recurred.",
            recurrence_count=1,
            source_type_count=1,
            impact="medium",
            confidence=1.2,
            confidence_version="opportunity_confidence_v1",
            status="insufficient_evidence",
        )


def test_runtime_and_code_candidates_reject_patch_preview() -> None:
    for target_type in ("runtime", "code"):
        with pytest.raises(ValidationError, match="only skill candidates"):
            _candidate(target_type=target_type, patch_preview="--- a/file\n+++ b/file")


def test_skill_candidate_accepts_patch_preview_and_not_run_check() -> None:
    candidate = _candidate(
        target_type="skill",
        target_ref="realtime_web_search",
        patch_preview="--- a/skills/realtime_web_search/SKILL.md\n+++ b/skills/realtime_web_search/SKILL.md",
    )

    assert candidate.evaluation.checks[0].status == "not_run"
    assert candidate.status == "proposed"


def test_human_decision_uses_terminal_decision_status_only() -> None:
    decision = ImprovementDecision(
        decision_id="decision_1",
        candidate_id="candidate_1",
        decision="accepted",
        decided_at=datetime.now(timezone.utc),
        reviewer="owner",
    )

    assert decision.decision == "accepted"
    with pytest.raises(ValidationError):
        ImprovementDecision(
            decision_id="decision_2",
            candidate_id="candidate_1",
            decision="ready_for_review",
            reviewer="owner",
        )


@pytest.mark.parametrize("skill_id", ["../escape", "/tmp/escape", "nested/skill", ".."])
def test_skill_targets_reject_path_traversal(skill_id: str) -> None:
    with pytest.raises(ValidationError, match="safe single path segment"):
        ImprovementTargetRef(target_type="skill", target_ref=skill_id)


def _candidate(
    *,
    target_type: str,
    target_ref: str = "assistant_loop",
    patch_preview: str | None = None,
) -> ImprovementCandidate:
    return ImprovementCandidate(
        candidate_id="candidate_1",
        opportunity_id="op_1",
        target_type=target_type,
        target_ref=target_ref,
        evidence_refs=["ev_1"],
        failure_pattern="Repeated failure.",
        root_cause_hypothesis="The current guidance may be incomplete.",
        proposed_change="Clarify the governed behavior.",
        affected_locations=[target_ref],
        expected_benefit="Reduce repeated failures.",
        patch_preview=patch_preview,
        acceptance_criteria=["The targeted regression case passes."],
        suggested_test_suite_ids=["assistant_loop"],
        risk_level="medium",
        limitations=["Requires human review."],
        evaluation=CandidateEvaluation(
            checks=[CandidateCheck(check_name="schema_valid", status="not_run", summary="Pending.")],
            regression_suites=["assistant_loop"],
            blocked_reasons=[],
        ),
        status="proposed",
    )

from pathlib import Path

from assistant_agent.schemas.improvement import (
    CandidateEvaluation,
    ImprovementCandidate,
    ImprovementDecision,
    ImprovementEvidence,
    ImprovementOpportunity,
)
from assistant_agent.services.improvement.registry import JsonlImprovementRegistry


def test_registry_is_idempotent_across_instances(tmp_path: Path) -> None:
    evidence = _evidence()
    first = JsonlImprovementRegistry(tmp_path)

    assert first.append_evidence([evidence, evidence]) == 1
    second = JsonlImprovementRegistry(tmp_path)
    assert second.append_evidence([evidence]) == 0
    assert len((tmp_path / "evidence.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_decisions_are_separate_and_do_not_rewrite_candidates(tmp_path: Path) -> None:
    registry = JsonlImprovementRegistry(tmp_path)
    opportunity = _opportunity()
    registry.append_opportunities([opportunity])
    before = (tmp_path / "opportunities.jsonl").read_text(encoding="utf-8")

    written = registry.record_decision(
        ImprovementDecision(
            decision_id="decision_1",
            candidate_id="candidate_1",
            decision="accepted",
            reviewer="owner",
        )
    )

    assert written is True
    assert (tmp_path / "opportunities.jsonl").read_text(encoding="utf-8") == before
    assert (tmp_path / "decisions.jsonl").exists()


def test_malformed_existing_lines_are_skipped_with_issue(tmp_path: Path) -> None:
    (tmp_path / "evidence.jsonl").write_text("not-json\n", encoding="utf-8")
    registry = JsonlImprovementRegistry(tmp_path)

    assert registry.append_evidence([_evidence()]) == 1
    assert "registry_invalid_json" in registry.issues


def test_registry_rejects_unsafe_candidate_payload(tmp_path: Path) -> None:
    registry = JsonlImprovementRegistry(tmp_path)
    candidate = ImprovementCandidate(
        candidate_id="candidate_unsafe",
        opportunity_id="op_1",
        target_type="runtime",
        target_ref="assistant_loop",
        evidence_refs=["ev_1"],
        failure_pattern="Loop failure.",
        root_cause_hypothesis="Bearer private-token",
        proposed_change="Review the loop.",
        expected_benefit="Reduce failures.",
        acceptance_criteria=["The regression test passes."],
        suggested_test_suite_ids=["assistant_loop"],
        risk_level="medium",
        evaluation=CandidateEvaluation(),
    )

    assert registry.append_candidates([candidate]) == 0
    assert "registry_unsafe_record" in registry.issues


def _evidence() -> ImprovementEvidence:
    return ImprovementEvidence(
        evidence_id="ev_1",
        source_type="trajectory",
        source_ref="trace:1",
        symptom_code="assistant_loop_limit_reached",
        summary="Loop guard stopped the run.",
    )


def _opportunity() -> ImprovementOpportunity:
    return ImprovementOpportunity(
        opportunity_id="op_1",
        target_type="runtime",
        target_ref="assistant_loop",
        evidence_refs=["ev_1"],
        pattern_code="assistant_loop_limit_reached",
        problem_statement="The loop limit recurred.",
        recurrence_count=2,
        source_type_count=1,
        impact="medium",
        confidence=0.7,
        status="ready_for_proposal",
    )

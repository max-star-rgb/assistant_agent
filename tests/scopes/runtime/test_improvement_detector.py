from datetime import datetime, timedelta, timezone

from assistant_agent.schemas.improvement import ImprovementEvidence, ImprovementTargetRef
from assistant_agent.services.improvement.detector import detect_opportunities


def test_one_ordinary_observation_is_insufficient() -> None:
    opportunities = detect_opportunities([_evidence("ev1", "trace:1")])

    assert len(opportunities) == 1
    assert opportunities[0].status == "insufficient_evidence"
    assert "independent_evidence_required" in opportunities[0].blocked_reasons


def test_two_independent_sources_create_ready_deterministic_opportunity() -> None:
    evidence = [_evidence("ev1", "trace:1"), _evidence("ev2", "trace:2")]

    first = detect_opportunities(evidence)
    second = detect_opportunities(list(reversed(evidence)))

    assert first[0].status == "ready_for_proposal"
    assert first[0].recurrence_count == 2
    assert first[0].opportunity_id == second[0].opportunity_id
    assert first[0].confidence == second[0].confidence


def test_duplicate_source_ref_counts_once() -> None:
    opportunities = detect_opportunities(
        [_evidence("ev1", "trace:1"), _evidence("ev2", "trace:1")]
    )

    assert opportunities[0].recurrence_count == 1
    assert opportunities[0].status == "insufficient_evidence"


def test_one_high_severity_eval_failure_is_ready() -> None:
    evidence = _evidence(
        "ev1",
        "eval:case1",
        source_type="eval_failure",
        target_type="skill",
        target_ref="realtime_web_search",
        symptom_code="skill_tool_not_selected_in_eval",
        severity="high",
    )

    opportunity = detect_opportunities([evidence])[0]

    assert opportunity.status == "ready_for_proposal"
    assert opportunity.target_type == "skill"


def test_skill_semantic_pattern_without_eval_is_blocked() -> None:
    evidence = _evidence(
        "ev1",
        "trace:1",
        target_type="skill",
        target_ref="realtime_web_search",
        symptom_code="skill_tool_not_selected_in_eval",
        severity="high",
    )

    opportunity = detect_opportunities([evidence])[0]

    assert opportunity.status == "insufficient_evidence"
    assert "skill_eval_evidence_required" in opportunity.blocked_reasons


def test_code_target_requires_module_or_symbol_and_filter_is_applied() -> None:
    code_evidence = _evidence(
        "ev1",
        "pytest:case1",
        source_type="test_failure",
        target_type="code",
        target_ref="assistant_agent.agent.runtime",
        symptom_code="deterministic_test_regression",
        severity="high",
        attributes={},
    )
    runtime_evidence = _evidence("ev2", "trace:2")

    code_only = detect_opportunities([code_evidence, runtime_evidence], target_type="code")

    assert len(code_only) == 1
    assert code_only[0].status == "insufficient_evidence"
    assert "concrete_code_location_required" in code_only[0].blocked_reasons


def test_evidence_older_than_analysis_window_is_ignored() -> None:
    now = datetime.now(timezone.utc)
    old = _evidence("old", "trace:old").model_copy(update={"occurred_at": now - timedelta(days=31)})
    recent = _evidence("recent", "trace:recent").model_copy(update={"occurred_at": now})

    opportunities = detect_opportunities([old, recent], now=now, max_age_days=30)

    assert opportunities[0].recurrence_count == 1
    assert opportunities[0].status == "insufficient_evidence"


def _evidence(
    evidence_id: str,
    source_ref: str,
    *,
    source_type: str = "trajectory",
    target_type: str = "runtime",
    target_ref: str = "assistant_loop",
    symptom_code: str = "assistant_loop_limit_reached",
    severity: str = "medium",
    attributes: dict | None = None,
) -> ImprovementEvidence:
    return ImprovementEvidence(
        evidence_id=evidence_id,
        source_type=source_type,
        source_ref=source_ref,
        target_hints=[ImprovementTargetRef(target_type=target_type, target_ref=target_ref)],
        symptom_code=symptom_code,
        summary="A prompt-safe failure summary.",
        severity=severity,
        attributes=attributes or {},
    )

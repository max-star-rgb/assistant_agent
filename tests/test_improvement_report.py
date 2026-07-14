from assistant_agent.schemas.improvement import (
    CandidateCheck,
    CandidateEvaluation,
    ImprovementCandidate,
    ImprovementRunReport,
)
from assistant_agent.services.improvement.report import render_improvement_report


def test_report_contains_review_sections_diff_and_non_mutation_statement() -> None:
    candidate = ImprovementCandidate(
        candidate_id="candidate_1",
        opportunity_id="op_1",
        target_type="skill",
        target_ref="realtime_web_search",
        evidence_refs=["ev_1"],
        failure_pattern="Search tool was missed in eval.",
        root_cause_hypothesis="Usage guidance may be too broad.",
        proposed_change="Clarify when current web lookup is required.",
        expected_benefit="Improve governed search selection.",
        patch_preview="--- a/skills/realtime_web_search/SKILL.md\n+++ b/skills/realtime_web_search/SKILL.md\n",
        acceptance_criteria=["The offline expected-tool eval passes."],
        suggested_test_suite_ids=["skill_tool_contract"],
        risk_level="low",
        limitations=["Human review is required."],
        evaluation=CandidateEvaluation(
            checks=[CandidateCheck(check_name="schema_valid", status="passed", summary="Valid.")],
            regression_suites=["skill_tool_contract"],
            blocked_reasons=[],
            score=0.8,
            ready_for_review=True,
        ),
        status="ready_for_review",
    )

    rendered = render_improvement_report(
        ImprovementRunReport(run_id="lab_1", candidates=[candidate])
    )

    assert "# Improvement Lab Report" in rendered
    assert "## Candidates" in rendered
    assert "```diff" in rendered
    assert "No production mutation occurred" in rendered
    assert "pytest" in rendered


def test_report_uses_safe_fence_when_patch_contains_backticks() -> None:
    candidate = ImprovementCandidate(
        candidate_id="candidate_fence",
        opportunity_id="op_1",
        target_type="skill",
        target_ref="realtime_web_search",
        evidence_refs=["ev_1"],
        failure_pattern="Skill regression.",
        root_cause_hypothesis="Guidance is incomplete.",
        proposed_change="Clarify guidance.",
        expected_benefit="Improve selection.",
        patch_preview="+```\n+spoofed heading\n+```\n",
        acceptance_criteria=["The skill regression test passes."],
        suggested_test_suite_ids=["skill_tool_contract"],
        risk_level="low",
        evaluation=CandidateEvaluation(),
    )

    rendered = render_improvement_report(ImprovementRunReport(run_id="lab_fence", candidates=[candidate]))

    assert "````diff" in rendered


def test_report_fence_accounts_for_diff_context_prefix() -> None:
    candidate = ImprovementCandidate(
        candidate_id="candidate_context_fence",
        opportunity_id="op_1",
        target_type="skill",
        target_ref="realtime_web_search",
        evidence_refs=["ev_1"],
        failure_pattern="Skill regression.",
        root_cause_hypothesis="Guidance is incomplete.",
        proposed_change="Clarify guidance.",
        expected_benefit="Improve selection.",
        patch_preview=" ````\n",
        acceptance_criteria=["The skill regression test passes."],
        suggested_test_suite_ids=["skill_tool_contract"],
        risk_level="low",
        evaluation=CandidateEvaluation(),
    )

    rendered = render_improvement_report(
        ImprovementRunReport(run_id="lab_context_fence", candidates=[candidate])
    )

    assert "`````diff" in rendered


def test_report_fence_accounts_for_indented_context_and_trailing_space() -> None:
    candidate = ImprovementCandidate(
        candidate_id="candidate_indented_fence",
        opportunity_id="op_1",
        target_type="skill",
        target_ref="realtime_web_search",
        evidence_refs=["ev_1"],
        failure_pattern="Skill regression.",
        root_cause_hypothesis="Guidance is incomplete.",
        proposed_change="Clarify guidance.",
        expected_benefit="Improve selection.",
        patch_preview="   ```   \n",
        acceptance_criteria=["The skill regression test passes."],
        suggested_test_suite_ids=["skill_tool_contract"],
        risk_level="low",
        evaluation=CandidateEvaluation(),
    )

    rendered = render_improvement_report(
        ImprovementRunReport(run_id="lab_indented_fence", candidates=[candidate])
    )

    assert "````diff" in rendered

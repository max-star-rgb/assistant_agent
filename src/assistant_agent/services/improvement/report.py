"""Developer-readable rendering for prompt-safe improvement results."""

from __future__ import annotations

from assistant_agent.schemas.improvement import ImprovementRunReport
from assistant_agent.services.improvement.evaluator import resolved_test_commands
from assistant_agent.services.provider_errors import sanitize_error_message


def render_improvement_report(report: ImprovementRunReport) -> str:
    """Render a bounded Markdown review report without raw source payloads."""

    lines = [
        "# Improvement Lab Report",
        "",
        f"Run ID: `{_safe(report.run_id)}`",
        f"Analysis started: `{report.started_at.isoformat()}`",
        f"Analysis completed: `{report.completed_at.isoformat() if report.completed_at else 'not_completed'}`",
        f"Evidence window: {report.analysis_max_age_days} days; cutoff `{report.analysis_cutoff.isoformat()}`",
        f"Evidence: {len(report.evidence)}",
        f"Rejected evidence sources: {sum(issue.code.startswith('evidence_') for issue in report.issues)}",
        f"Opportunities: {len(report.opportunities)}",
        f"Candidates: {len(report.candidates)}",
        "",
        "## Evidence Summary",
        "",
    ]
    if not report.evidence:
        lines.append("No accepted evidence records.")
    for item in report.evidence:
        lines.append(
            f"- `{_safe(item.evidence_id)}` — `{_safe(item.symptom_code)}` "
            f"from `{_safe(item.source_ref)}`"
        )

    lines.extend(["", "## Opportunities", ""])
    if not report.opportunities:
        lines.append("No improvement opportunities detected.")
    for item in report.opportunities:
        lines.append(
            f"- `{_safe(item.opportunity_id)}` — **{item.status}** — "
            f"`{item.target_type}:{_safe(item.target_ref)}` — recurrence {item.recurrence_count}, "
            f"confidence {item.confidence:.2f}"
        )
        lines.append(f"  - Pattern: `{_safe(item.pattern_code)}`")
        lines.append(f"  - Problem: {_safe(item.problem_statement)}")
        lines.append(f"  - Evidence: {', '.join(f'`{_safe(ref)}`' for ref in item.evidence_refs)}")
        if item.blocked_reasons:
            lines.append(f"  - Blocked: {', '.join(_safe(reason) for reason in item.blocked_reasons)}")

    lines.extend(["", "## Candidates", ""])
    if not report.candidates:
        lines.append("No candidates were generated.")
    for candidate in report.candidates:
        lines.extend(
            [
                f"### `{_safe(candidate.candidate_id)}`",
                "",
                f"- Target: `{candidate.target_type}:{_safe(candidate.target_ref)}`",
                f"- Status: **{candidate.status}**",
                f"- Risk: `{candidate.risk_level}`",
                f"- Score: `{candidate.evaluation.score}`",
                f"- Evidence: {', '.join(f'`{_safe(ref)}`' for ref in candidate.evidence_refs)}",
                f"- Failure pattern: {_safe(candidate.failure_pattern)}",
                f"- Affected locations: {', '.join(_safe(value) for value in candidate.affected_locations) or 'none'}",
                f"- Hypothesis: {_safe(candidate.root_cause_hypothesis)}",
                f"- Proposed change: {_safe(candidate.proposed_change)}",
                f"- Expected benefit: {_safe(candidate.expected_benefit)}",
                f"- Suggested suite IDs: {', '.join(_safe(value) for value in candidate.suggested_test_suite_ids) or 'none'}",
                "",
                "Acceptance criteria:",
                "",
            ]
        )
        lines.extend(f"- {_safe(item)}" for item in candidate.acceptance_criteria)
        lines.extend(["", "Evaluation gates:", ""])
        for check in candidate.evaluation.checks:
            lines.append(f"- `{_safe(check.check_name)}`: **{check.status}** — {_safe(check.summary)}")
        if candidate.evaluation.blocked_reasons:
            lines.append(
                f"- Blocked reasons: {', '.join(_safe(reason) for reason in candidate.evaluation.blocked_reasons)}"
            )
        commands = resolved_test_commands(candidate)
        if commands:
            lines.extend(["", "Suggested fixed test commands:", ""])
            lines.extend(f"- `{_safe(command)}`" for command in commands)
        if candidate.patch_preview:
            fence = _diff_fence(candidate.patch_preview)
            lines.extend(["", "Patch preview:", "", f"{fence}diff", candidate.patch_preview.rstrip(), fence])
        if candidate.limitations:
            lines.extend(["", "Limitations:", ""])
            lines.extend(f"- {_safe(item)}" for item in candidate.limitations)
        lines.append("")

    lines.extend(["## Allowlisted Validation", ""])
    if not report.validation_results:
        lines.append("No allowlisted validation suites were run.")
    for result in report.validation_results:
        lines.append(
            f"- `{_safe(result.suite_id)}` — **{result.status}** — `{_safe(result.command)}` — {_safe(result.summary)}"
        )
    lines.extend(["", "## Issues", ""])
    if not report.issues:
        lines.append("No lab infrastructure issues were recorded.")
    for issue in report.issues:
        source = f" (`{_safe(issue.source_ref)}`)" if issue.source_ref else ""
        lines.append(f"- `{_safe(issue.code)}`{source} — {_safe(issue.summary)}")
    lines.extend(
        [
            "",
            "## Mutation Statement",
            "",
            "No production mutation occurred. This report contains review artifacts only.",
            "",
        ]
    )
    return "\n".join(lines)


def _safe(value: str) -> str:
    return sanitize_error_message(value)


def _diff_fence(value: str) -> str:
    longest = 0
    for line in value.splitlines():
        candidate = line[1:] if line[:1] in {"+", "-", " "} else line
        stripped = candidate.strip()
        if stripped and set(stripped) == {"`"}:
            longest = max(longest, len(stripped))
    return "`" * max(3, longest + 1)

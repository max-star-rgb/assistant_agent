"""Deterministic baseline rendering for runtime audit artifacts."""

from __future__ import annotations

from collections import Counter

from assistant_agent.observability.runtime_audit.models import CodexAuditReport, RuntimeAuditBundle
from assistant_agent.providers.provider_errors import sanitize_error_message


def render_deterministic_report(bundle: RuntimeAuditBundle) -> str:
    """Render facts even when the optional Codex analysis is unavailable."""

    by_category = Counter(item.category for item in bundle.findings)
    quality_failures = sum(item.quality_failure for item in bundle.findings)
    lines = [
        "# AgentRuntime Audit Report",
        "",
        f"Audit run: `{_safe(bundle.audit_run_id)}`",
        f"Window: `{bundle.window_start.isoformat()}` — `{bundle.window_end.isoformat()}`",
        f"Collected: `{bundle.collected_at.isoformat()}`",
        "",
        "## Coverage",
        "",
        f"- Langfuse source available: {str(bundle.coverage.langfuse_source_available).lower()}",
        f"- Langfuse traces: {bundle.coverage.langfuse_trace_count}",
        f"- Local completeness manifests: {bundle.coverage.local_trace_count}",
        f"- Matched trace IDs: {bundle.coverage.matched_trace_count}",
        f"- Missing Langfuse exports: {bundle.coverage.missing_export_count}",
        f"- Local completeness source available: {str(bundle.coverage.local_source_available).lower()}",
        "",
        "## Deterministic Findings",
        "",
        f"Quality failures: {quality_failures}; coverage: {by_category['coverage']}; "
        f"memory: {by_category['memory']}; tool: {by_category['tool']}; "
        f"infrastructure: {by_category['infrastructure']}.",
        "",
    ]
    if not bundle.findings:
        lines.append("No deterministic findings in this window.")
    for finding in bundle.findings:
        target = "/".join(
            value
            for value in (finding.trace_id, finding.observation_id, finding.score_name)
            if value
        )
        suffix = f" (`{_safe(target)}`)" if target else ""
        lines.append(
            f"- **{finding.severity}** `{_safe(finding.code)}`{suffix}: "
            f"{_safe(finding.summary)}"
        )
    lines.extend(
        [
            "",
            "## Mutation Statement",
            "",
            "No production, code, Langfuse, or memory mutation was performed.",
            "",
            "Codex recommendations, when enabled, are review-only and must be applied manually.",
            "",
        ]
    )
    return "\n".join(lines)


def render_codex_report(report: CodexAuditReport) -> str:
    """Render a validated Codex JSON report for human review."""

    lines = [
        "# Codex AgentRuntime Audit Report",
        "",
        f"Audit run: `{_safe(report.audit_run_id)}`",
        "",
        "## Executive Summary",
        "",
        _safe(report.executive_summary),
        "",
        "## Coverage Assessment",
        "",
        _safe(report.coverage_assessment),
    ]
    for title, values in (
        ("Quality Findings", report.quality_findings),
        ("Memory Findings", report.memory_findings),
        ("Tool Trajectory Findings", report.tool_trajectory_findings),
        ("Infrastructure Findings", report.infrastructure_findings),
    ):
        lines.extend(["", f"## {title}", ""])
        lines.extend(f"- {_safe(value)}" for value in values)
        if not values:
            lines.append("No findings reported.")
    lines.extend(["", "## Recommendations", ""])
    if not report.recommendations:
        lines.append("No change recommendation was produced.")
    for item in report.recommendations:
        lines.extend(
            [
                f"### [{item.priority}] {_safe(item.summary)}",
                "",
                f"- Area: `{item.area}`",
                f"- Evidence: {', '.join(f'`{_safe(ref)}`' for ref in item.evidence_refs) or 'none'}",
                f"- Suggested change: {_safe(item.suggested_change)}",
                f"- Validation: {_safe(item.validation)}",
                "",
            ]
        )
    lines.extend(["## Limitations", ""])
    lines.extend(f"- {_safe(value)}" for value in report.limitations)
    if not report.limitations:
        lines.append("No additional limitation reported.")
    lines.extend(
        [
            "",
            "## Mutation Statement",
            "",
            "No production, code, Langfuse, or memory mutation was performed.",
            "",
        ]
    )
    return "\n".join(lines)


def _safe(value: str) -> str:
    return sanitize_error_message(value)

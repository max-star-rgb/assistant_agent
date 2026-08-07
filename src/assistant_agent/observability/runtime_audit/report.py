"""Deterministic baseline rendering for runtime audit artifacts."""

from __future__ import annotations

from collections import Counter
from datetime import date
import re

from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditIssue,
    DailyCodexAuditReport,
)
from assistant_agent.observability.runtime_audit.models import CodexAuditReport, RuntimeAuditBundle
from assistant_agent.observability.runtime_audit.safety import (
    sanitize_runtime_audit_text,
)


_MACHINE_EVIDENCE_REF = re.compile(
    r"(?<![A-Za-z0-9_])(?:trace|code|test|observation|run|score):[A-Za-z0-9._/:@+=-]+"
)
_UUID = re.compile(
    r"(?<![A-Za-z0-9_])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_MARKDOWN_CONTROL = re.compile(r"([\\`*_{}\[\]<>#+()\-.!|])")
_UNSAFE_ISSUE_KEY = re.compile(r"[^A-Za-z0-9._/:@+=-]")


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


def render_daily_codex_report(
    report: DailyCodexAuditReport,
    *,
    issues: list[DailyAuditIssue] | None = None,
) -> str:
    """Render the daily Codex contract as a plain-language Chinese report."""

    issue_view = report.issues if issues is None else issues
    decision_issues = [
        issue
        for issue in issue_view
        if issue.status in {"open", "regressed"}
    ]
    addressed_issues = [
        issue for issue in issue_view if issue.status == "code_addressed"
    ]
    verified_issues = [
        issue for issue in issue_view if issue.status == "runtime_verified"
    ]
    uncertain_issues = [
        issue for issue in issue_view if issue.status == "uncertain"
    ]
    lines = [
        f"# {report.audit_date.isoformat()} 运行审计日报",
        "",
        "## 昨天是否需要你处理",
        "",
        _plain_text(report.daily_summary),
        "",
        "## 需要处理",
        "",
    ]
    _append_issue_guidance(lines, decision_issues, empty_message="没有需要你处理的新问题。")
    lines.extend(["", "## 暂时观察", ""])
    _append_issue_guidance(
        lines,
        uncertain_issues,
        empty_message="没有需要暂时观察的新问题。",
        include_suggested_change=False,
    )
    lines.extend(["", "## 已修改，等待实际验证", ""])
    _append_issue_guidance(
        lines,
        addressed_issues,
        empty_message="没有已修改、等待实际验证的问题。",
        include_suggested_change=False,
    )
    lines.extend(["", "## 昨天确认恢复", ""])
    _append_issue_guidance(
        lines,
        verified_issues,
        empty_message="昨天没有确认恢复的问题。",
        include_suggested_change=False,
    )
    lines.extend(
        [
            "",
            "## 技术附录（通常不用看）",
            "",
            f"- 扫描概况：{_plain_text(report.activity_summary)}",
            f"- 记忆检查：{_plain_text(report.memory_summary)}",
            f"- 审计系统：{_plain_text(report.infrastructure_summary)}",
        ]
    )
    if report.limitations:
        lines.append("- 还不能确定的部分：")
        lines.extend(
            f"  - {_plain_text(value, fallback='未提供限制说明。')}"
            for value in report.limitations
        )
    lines.extend(["", "## 证据附录", ""])
    _append_evidence_appendix(lines, issue_view)
    return "\n".join(lines) + "\n"


def render_empty_daily_report(
    audit_date: date,
    *,
    langfuse_available: bool,
    local_available: bool,
    issues: list[DailyAuditIssue] | None = None,
) -> str:
    """Render a successful no-activity day without invoking Codex."""

    availability = [
        "Langfuse 证据源可用" if langfuse_available else "Langfuse 证据源不可用",
        "本地完整性证据可用" if local_available else "本地完整性证据不可用",
    ]
    if not (langfuse_available and local_available):
        return render_failed_daily_report(
            audit_date,
            f"证据源不完整：{'；'.join(availability)}。无法确认昨日是否无可审计对话。",
        )
    lines = [
        f"# {audit_date.isoformat()} 运行审计日报",
        "",
        "## 一句话结论",
        "",
        "昨日无可审计对话，审计任务运行正常。",
        "",
        "## 系统运行情况",
        "",
        f"- {'；'.join(availability)}。",
    ]
    active = [
        issue
        for issue in (issues or [])
        if issue.status in {"open", "regressed", "code_addressed", "uncertain"}
    ]
    if active:
        lines.extend(["", "## 连续问题状态", ""])
        for issue in active:
            label = {
                "open": "仍待决定",
                "regressed": "再次出现",
                "code_addressed": "已处理，等待自然验证",
                "uncertain": "需要继续观察",
            }[issue.status]
            lines.append(
                f"- `{_render_issue_key(issue.issue_key)}`：{_plain_text(issue.title, fallback='未命名问题')}（{label}）。"
            )
    lines.append("")
    return "\n".join(lines)


def render_no_anomaly_daily_report(
    audit_date: date,
    *,
    trace_count: int,
    issues: list[DailyAuditIssue] | None = None,
) -> str:
    """Render a nonempty day whose deterministic third layer has no anomaly."""

    lines = [
        f"# {audit_date.isoformat()} 运行审计日报",
        "",
        "## 结论",
        "",
        f"昨天扫描了 {trace_count} 条对话，没有发现需要处理的异常，因此没有调用 Codex。",
    ]
    active = [
        issue
        for issue in (issues or [])
        if issue.status in {"open", "regressed", "code_addressed", "uncertain"}
    ]
    if active:
        lines.extend(["", "## 以前的问题", ""])
        for issue in active:
            lines.append(f"- {_plain_text(issue.title, fallback='未命名问题')}：仍需后续观察。")
    lines.append("")
    return "\n".join(lines)


def render_failed_daily_report(audit_date: date, error_summary: str) -> str:
    """Render a safe and explicit daily-audit failure for maintainers."""

    return "\n".join(
        [
            f"# {audit_date.isoformat()} 运行审计日报",
            "",
            "## 一句话结论",
            "",
            "审计未完成，无法对昨日运行情况给出结论。",
            "",
            "## 系统运行情况",
            "",
            f"- 失败摘要：{_failed_plain_text(error_summary)}",
            "",
        ]
    )


def _append_issue_guidance(
    lines: list[str],
    issues: list[DailyAuditIssue],
    *,
    empty_message: str,
    include_suggested_change: bool = True,
) -> None:
    if not issues:
        lines.append(empty_message)
        return
    for issue in issues:
        lines.extend([f"### {_plain_text(issue.title, fallback='未命名问题')}", ""])
        lines.append(
            "- 发生了什么："
            f"{_plain_text(issue.plain_summary, fallback='暂无问题说明。')} "
            f"{_plain_text(issue.user_impact, fallback='用户影响尚不明确。')}"
        )
        if include_suggested_change:
            lines.append(
                f"- 建议怎么做：{_plain_text(issue.suggested_change, fallback='暂无具体修改建议。')}"
            )
        lines.append(
            f"- 怎么确认：{_plain_text(issue.validation, fallback='尚未提供验证方式。')}"
        )
        lines.append("")
    lines.pop()


def _append_evidence_appendix(lines: list[str], issues: list[DailyAuditIssue]) -> None:
    has_evidence = False
    for issue in issues:
        references = [
            *issue.trace_evidence_refs,
            *issue.code_evidence_refs,
            *issue.runtime_verification_refs,
        ]
        if not references:
            continue
        has_evidence = True
        lines.append(
            f"- `{_render_issue_key(issue.issue_key)}`："
            f"{', '.join(f'`{_safe(ref)}`' for ref in references)}"
        )
    if not has_evidence:
        lines.append("没有可附上的机器证据。")


def _safe(value: str) -> str:
    return sanitize_runtime_audit_text(value)


def _render_issue_key(value: str) -> str:
    return _UNSAFE_ISSUE_KEY.sub("_", sanitize_runtime_audit_text(value))


def _plain_text(value: str, *, fallback: str = "") -> str:
    """Render untrusted model text as one escaped Markdown-safe human sentence."""

    if not value.strip():
        return fallback
    sanitized = sanitize_runtime_audit_text(value).strip()
    without_machine_refs = _MACHINE_EVIDENCE_REF.sub("机器证据见附录", sanitized)
    without_machine_ids = _UUID.sub("机器证据见附录", without_machine_refs)
    collapsed = " ".join(without_machine_ids.split())
    if not collapsed:
        return fallback
    return _MARKDOWN_CONTROL.sub(r"\\\1", collapsed)


def _failed_plain_text(error_summary: str) -> str:
    """Render a failure summary without retaining URL-embedded user credentials."""

    return _plain_text(error_summary, fallback="未提供失败摘要。")

"""Deterministic baseline rendering for runtime audit artifacts."""

from __future__ import annotations

from collections import Counter
from datetime import date, timezone
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditIssue,
    DailyCodexAuditReport,
)
from assistant_agent.observability.runtime_audit.models import (
    CodexAuditReport,
    LangfuseTraceSnapshot,
    RuntimeAuditBundle,
)
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
_GIT_SHA = re.compile(
    r"(?<![0-9a-f])(?=[0-9a-f]{7,40}(?![0-9a-f]))(?=[0-9a-f]*[a-f])"
    r"[0-9a-f]{7,40}(?![0-9a-f])",
    re.IGNORECASE,
)
_TEST_PATH = re.compile(r"(?<![A-Za-z0-9_])tests(?:/[A-Za-z0-9._@+=-]+)+")
_INTERNAL_TERM = re.compile(
    r"(?<![A-Za-z0-9_])(?:open|regressed|uncertain|code_addressed|"
    r"runtime_verified|owning module|grounding)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_MARKDOWN_CONTROL = re.compile(r"([\\`*_{}\[\]<>#+()\-.!|])")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_ISSUE_TRACE_REFERENCES = 3


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
    traces: list[LangfuseTraceSnapshot] | None = None,
) -> str:
    """Render validated audit facts as a conversational Chinese reply."""

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
        f"# {report.audit_date.isoformat()} 运行审计",
        "",
        _plain_text(report.daily_summary, max_chars=160),
    ]
    if decision_issues:
        lines.extend(["", "## 你现在需要处理的", ""])
        _append_conversational_issues(
            lines,
            decision_issues,
            include_advice=True,
            traces=traces,
        )
    if addressed_issues:
        lines.extend(
            [
                "",
                "## 这些已经改了，先观察",
                "",
                "下面这些已经有针对性的代码调整。现在不建议继续修改，先等后续真实对话验证效果。",
                "",
            ]
        )
        _append_conversational_issues(lines, addressed_issues, traces=traces)
    if verified_issues:
        lines.extend(["", "## 昨天已经确认恢复的", ""])
        _append_conversational_issues(lines, verified_issues, traces=traces)
    if uncertain_issues or report.limitations:
        lines.extend(["", "## 还有一些暂时不能下结论", ""])
        _append_conversational_issues(lines, uncertain_issues, traces=traces)
        if uncertain_issues and report.limitations:
            lines.append("")
        lines.extend(
            _plain_text(
                value,
                fallback="目前还缺少足够信息。",
                max_chars=100,
            )
            for value in report.limitations[:2]
        )
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
        f"# {audit_date.isoformat()} 运行审计",
        "",
        "昨天没有可审计对话。审计流程本身运行正常，目前没有需要你处理的新问题。",
    ]
    active = [
        issue
        for issue in (issues or [])
        if issue.status in {"open", "regressed", "code_addressed", "uncertain"}
    ]
    if active:
        lines.extend(["", "以前发现的问题还需要继续跟踪：", ""])
        lines.extend(
            f"- {_plain_text(issue.title, fallback='一个尚未命名的问题')}。"
            for issue in active
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
        f"# {audit_date.isoformat()} 运行审计",
        "",
        f"昨天检查了 {trace_count} 次运行，没有发现需要你处理的新问题。",
    ]
    active = [
        issue
        for issue in (issues or [])
        if issue.status in {"open", "regressed", "code_addressed", "uncertain"}
    ]
    if active:
        lines.extend(["", "以前发现的问题还需要继续跟踪：", ""])
        for issue in active:
            lines.append(f"- {_plain_text(issue.title, fallback='未命名问题')}：仍需后续观察。")
    lines.append("")
    return "\n".join(lines)


def render_failed_daily_report(audit_date: date, error_summary: str) -> str:
    """Render a safe and explicit daily-audit failure for maintainers."""

    return "\n".join(
        [
            f"# {audit_date.isoformat()} 运行审计",
            "",
            "审计没有完成，所以暂时无法判断昨天的运行情况。",
            "",
            _failed_plain_text(error_summary),
            "",
        ]
    )


def _append_conversational_issues(
    lines: list[str],
    issues: list[DailyAuditIssue],
    *,
    include_advice: bool = False,
    traces: list[LangfuseTraceSnapshot] | None = None,
) -> None:
    for issue in issues:
        lines.extend(
            [
                f"### {_plain_text(issue.title, fallback='未命名问题', max_chars=50)}",
                "",
            ]
        )
        summary = _plain_text(
            issue.plain_summary,
            fallback="目前还没有足够信息说明具体原因。",
            max_chars=100 if include_advice else 80,
        )
        impact = _plain_text(
            issue.user_impact,
            fallback="对用户的具体影响暂时不明确。",
            max_chars=70 if include_advice else 60,
        )
        lines.append(f"{summary} {impact}")
        if include_advice:
            advice = _plain_text(
                issue.suggested_change,
                fallback="补齐相关运行证据，再决定是否需要改代码。",
                max_chars=80,
            )
            lines.extend(["", _direct_advice(advice)])
        related_traces = _issue_traces(issue, traces or [])
        if related_traces:
            lines.extend(["", "最近的相关记录：", ""])
            for index, trace in enumerate(related_traces, start=1):
                occurred_at = trace.timestamp.astimezone(_SHANGHAI).strftime(
                    "%Y-%m-%d %H:%M"
                )
                session_id = _inline_code(trace.session_id or "未提供")
                trace_id = _inline_code(trace.trace_id)
                trace_url = _safe_trace_url(trace.trace_url)
                assistant_turn = (
                    f"[`{trace_id}`](<{trace_url}>)"
                    if trace_url is not None
                    else f"`{trace_id}`（Langfuse 链接暂不可用）"
                )
                lines.extend(
                    [
                        f"{index}. {occurred_at}",
                        f"   Session：`{session_id}`",
                        f"   Assistant turn：{assistant_turn}",
                    ]
                )
        lines.append("")
    lines.pop()


def _issue_traces(
    issue: DailyAuditIssue,
    traces: list[LangfuseTraceSnapshot],
) -> list[LangfuseTraceSnapshot]:
    trace_ids = {
        trace_id
        for ref in [*issue.trace_evidence_refs, *issue.runtime_verification_refs]
        if (trace_id := _trace_id_from_evidence_ref(ref)) is not None
    }
    matching = [
        trace
        for trace in traces
        if trace.trace_id in trace_ids and trace.name == "assistant.turn"
    ]
    matching.sort(
        key=lambda trace: trace.timestamp.astimezone(timezone.utc),
        reverse=True,
    )
    return matching[:_MAX_ISSUE_TRACE_REFERENCES]


def _trace_id_from_evidence_ref(ref: str) -> str | None:
    if not ref.startswith("trace:"):
        return None
    trace_id = ref.removeprefix("trace:").split("/", 1)[0]
    return trace_id or None


def _inline_code(value: str) -> str:
    sanitized = sanitize_runtime_audit_text(value)
    return " ".join(sanitized.split()).replace("`", "′") or "未提供"


def _safe_trace_url(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if any(character.isspace() or ord(character) < 32 for character in value):
        return None
    if any(character in value for character in "<>"):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


def _safe(value: str) -> str:
    return sanitize_runtime_audit_text(value)


def _plain_text(
    value: str,
    *,
    fallback: str = "",
    max_chars: int | None = None,
) -> str:
    """Render untrusted model text as one escaped Markdown-safe human sentence."""

    if not value.strip():
        return fallback
    sanitized = sanitize_runtime_audit_text(value).strip()
    without_machine_refs = _MACHINE_EVIDENCE_REF.sub("", sanitized)
    without_machine_ids = _UUID.sub("", without_machine_refs)
    without_git_shas = _GIT_SHA.sub("", without_machine_ids)
    without_test_paths = _TEST_PATH.sub("", without_git_shas)
    without_internal_terms = _INTERNAL_TERM.sub("", without_test_paths)
    collapsed = " ".join(without_internal_terms.split())
    if not collapsed:
        return fallback
    if max_chars is not None and len(collapsed) > max_chars:
        collapsed = _truncate_human_text(collapsed, max_chars=max_chars)
    return _MARKDOWN_CONTROL.sub(r"\\\1", collapsed)


def _truncate_human_text(value: str, *, max_chars: int) -> str:
    if max_chars <= 1:
        return "…"
    candidate = value[: max_chars - 1].rstrip()
    sentence_end = max(candidate.rfind(mark) for mark in "。！？；")
    if sentence_end >= max_chars // 2:
        return candidate[: sentence_end + 1]
    return candidate.rstrip("，、；：。！？ ") + "…"


def _direct_advice(value: str) -> str:
    if value.startswith("我建议你"):
        return value
    if value.startswith("我建议"):
        return "我建议你" + value.removeprefix("我建议").removeprefix("你")
    if value.startswith("建议你"):
        return "我" + value
    return "我建议你" + value


def _failed_plain_text(error_summary: str) -> str:
    """Render a failure summary without retaining URL-embedded user credentials."""

    return _plain_text(error_summary, fallback="未提供失败摘要。")

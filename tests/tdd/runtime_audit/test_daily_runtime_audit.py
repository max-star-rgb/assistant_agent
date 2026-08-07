from __future__ import annotations

import json
import os
import subprocess
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_agent.observability.runtime_audit import storage as storage_module
from assistant_agent.observability.runtime_audit import cli as cli_module
from assistant_agent.observability.runtime_audit.cli import (
    _parser,
    _resolve_bundle_path,
    main,
)
from assistant_agent.observability.runtime_audit.collector import collect_runtime_audit
from assistant_agent.observability.runtime_audit.daily_runner import (
    recover_pending_daily_commits,
    run_one_daily_audit,
    run_pending_daily_audits,
)
from assistant_agent.observability.runtime_audit.daily_window import (
    pending_audit_dates,
    previous_day_window,
    window_for_date,
)
from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditAttempt,
    DailyAuditIssue,
    IssueRegistry,
)
from assistant_agent.observability.runtime_audit import daily_models as daily_models_module
from assistant_agent.observability.runtime_audit import report as report_module
from assistant_agent.observability.runtime_audit.issues import (
    merge_issue_registry,
)
from assistant_agent.observability.runtime_audit.langfuse_source import LangfuseSdkAuditSource
from assistant_agent.observability.runtime_audit.models import LangfuseTraceSnapshot
from assistant_agent.observability.runtime_audit.storage import RuntimeAuditArtifactStore


class FakeLangfuseSource:
    def __init__(self, traces: list[LangfuseTraceSnapshot] | Exception) -> None:
        self._traces = traces

    def list_traces(self, **_: datetime) -> list[LangfuseTraceSnapshot]:
        if isinstance(self._traces, Exception):
            raise self._traces
        return self._traces


def _daily_trace(trace_id: str = "trace-current") -> LangfuseTraceSnapshot:
    return LangfuseTraceSnapshot(
        trace_id=trace_id,
        name="assistant.turn",
        timestamp=datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
        observations=[],
        scores=[],
    )


def _daily_codex_report(
    *,
    audit_date: date = date(2026, 8, 5),
    issue: DailyAuditIssue | None = None,
) -> daily_models_module.DailyCodexAuditReport:
    return daily_models_module.DailyCodexAuditReport(
        audit_date=audit_date,
        daily_summary="发现一个需要处理的问题。",
        activity_summary="有一条可审计对话。",
        issues=[] if issue is None else [issue],
        memory_summary="未发现记忆问题。",
        infrastructure_summary="证据源正常。",
    )


def _runtime_audit_test_repo(
    tmp_path: Path,
    *,
    committed_at: str,
) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    test_path = repo / "tests/tdd/runtime_audit/test_regression.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_regression():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "audit@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Runtime Audit Test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    environment = dict(os.environ)
    environment.update(
        {"GIT_AUTHOR_DATE": committed_at, "GIT_COMMITTER_DATE": committed_at}
    )
    subprocess.run(
        ["git", "commit", "-m", "fix: add regression coverage"],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return repo, result.stdout.strip()


def test_human_daily_report_is_chinese_and_moves_machine_ids_to_appendix() -> None:
    """Would fail if the daily report buried user impact behind machine evidence."""

    report = daily_models_module.DailyCodexAuditReport(
        audit_date=date(2026, 8, 5),
        daily_summary="昨天有一个工具选择问题需要决定。",
        activity_summary="共 4 次对话，其中 1 次调用工具。",
        issues=[
            DailyAuditIssue(
                issue_key="tool.email_for_market_data",
                status="open",
                title="错误使用邮件搜索",
                plain_summary="助手把公开市场查询交给了邮箱搜索。",
                user_impact="用户可能得到无关结果。",
                suggested_change="收紧邮箱搜索的适用范围。",
                validation="等待后续同类自然请求。",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 5),
                trace_evidence_refs=["trace:abc/observation:def"],
            )
        ],
        memory_summary="未发现需要处理的记忆问题。",
        infrastructure_summary="Trace 导出正常。",
    )

    markdown = report_module.render_daily_codex_report(report)

    assert "## 需要你决定" in markdown
    assert "用户可能得到无关结果" in markdown
    assert markdown.index("## 证据附录") < markdown.index("trace:abc")
    assert "Executive Summary" not in markdown


def test_empty_day_report_is_short_and_explicitly_successful() -> None:
    """Would fail if an empty audit day were mistaken for an audit failure."""

    markdown = report_module.render_empty_daily_report(
        date(2026, 8, 5),
        langfuse_available=True,
        local_available=True,
    )

    assert "昨日无可审计对话" in markdown
    assert "审计任务运行正常" in markdown


@pytest.mark.parametrize(
    ("langfuse_available", "local_available"),
    [(False, True), (True, False), (False, False)],
)
def test_empty_day_report_requires_all_evidence_sources(
    langfuse_available: bool,
    local_available: bool,
) -> None:
    """Would fail if incomplete evidence were presented as a successful empty day."""

    markdown = report_module.render_empty_daily_report(
        date(2026, 8, 5),
        langfuse_available=langfuse_available,
        local_available=local_available,
    )

    assert "审计未完成" in markdown
    assert "昨日无可审计对话" not in markdown
    assert "审计任务运行正常" not in markdown


def test_daily_report_escapes_plain_text_and_keeps_body_machine_ids_in_appendix() -> None:
    """Would fail if report text could inject Markdown or expose evidence IDs in its body."""

    report = daily_models_module.DailyCodexAuditReport(
        audit_date=date(2026, 8, 5),
        daily_summary="结论见 trace:body-summary\n# 伪造标题",
        activity_summary="- [伪造链接](https://invalid.example)",
        issues=[
            DailyAuditIssue(
                issue_key="tool.email_for_market_data",
                status="open",
                title="## 伪造标题\n- [伪造链接](https://invalid.example)",
                plain_summary="问题见 code:body-summary",
                user_impact="影响尚不明。",
                suggested_change="建议见 test:body-test",
                validation="验证见 trace:body-validation",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 5),
                trace_evidence_refs=["trace:appendix-only"],
            )
        ],
        memory_summary="记忆见 trace:body-memory",
        infrastructure_summary="系统见 code:body-system",
    )

    body, appendix = report_module.render_daily_codex_report(report).split(
        "## 证据附录", maxsplit=1
    )

    assert "trace:appendix-only" not in body
    assert "trace:appendix-only" in appendix
    assert all(reference not in body for reference in (
        "trace:body-summary",
        "code:body-summary",
        "test:body-test",
        "trace:body-validation",
        "trace:body-memory",
        "code:body-system",
    ))
    assert body.count("机器证据见附录") >= 6
    assert "## 伪造标题\n- [伪造链接]" not in body
    assert "\\#\\# 伪造标题 \\- \\[伪造链接\\]\\(https://invalid\\.example\\)" in body


def test_daily_report_uses_clear_fallbacks_for_empty_optional_issue_text() -> None:
    """Would fail if optional issue text left blank human-facing report sections."""

    report = daily_models_module.DailyCodexAuditReport(
        audit_date=date(2026, 8, 5),
        daily_summary="有待确认的问题。",
        activity_summary="共 1 次对话。",
        issues=[
            DailyAuditIssue(
                issue_key="tool.email_for_market_data",
                status="open",
                title="问题标题",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 5),
            )
        ],
        memory_summary="未发现记忆问题。",
        infrastructure_summary="系统正常。",
    )

    markdown = report_module.render_daily_codex_report(report)

    assert "暂无问题说明。" in markdown
    assert "用户影响尚不明确。" in markdown
    assert "暂无具体修改建议。" in markdown
    assert "尚未提供验证方式。" in markdown


@pytest.mark.parametrize(
    "field_name",
    ["daily_summary", "activity_summary", "memory_summary", "infrastructure_summary"],
)
def test_daily_report_requires_nonblank_human_summaries(field_name: str) -> None:
    """Would fail if a blank core summary could produce an empty report section."""

    payload = {
        "audit_date": date(2026, 8, 5),
        "daily_summary": "结论",
        "activity_summary": "概况",
        "memory_summary": "记忆",
        "infrastructure_summary": "系统",
    }
    payload[field_name] = " \n "

    with pytest.raises(ValueError, match=f"{field_name} must not be blank"):
        daily_models_module.DailyCodexAuditReport(**payload)


def test_daily_report_trims_human_summaries() -> None:
    """Would fail if leading or trailing whitespace reached the daily renderer."""

    report = daily_models_module.DailyCodexAuditReport(
        audit_date=date(2026, 8, 5),
        daily_summary="  结论  ",
        activity_summary="  概况  ",
        memory_summary="  记忆  ",
        infrastructure_summary="  系统  ",
    )

    assert (
        report.daily_summary,
        report.activity_summary,
        report.memory_summary,
        report.infrastructure_summary,
    ) == ("结论", "概况", "记忆", "系统")


def test_failed_daily_report_states_failure_without_leaking_secrets() -> None:
    """Would fail if a failed audit looked successful or exposed credentials."""

    markdown = report_module.render_failed_daily_report(
        date(2026, 8, 5),
        "Authorization: Bearer private-token timed out",
    )

    assert "审计未完成" in markdown
    assert "private-token" not in markdown


def test_failed_daily_report_uses_the_same_plain_text_boundary() -> None:
    """Would fail if a failure summary could inject Markdown or expose a machine ID."""

    markdown = report_module.render_failed_daily_report(
        date(2026, 8, 5),
        "失败见 trace:failure\n# [伪造链接](https://invalid.example)",
    )

    assert "trace:failure" not in markdown
    assert "机器证据见附录" in markdown
    assert "# [伪造链接]" not in markdown
    assert "\\# \\[伪造链接\\]\\(https://invalid\\.example\\)" in markdown


def test_failed_daily_report_redacts_url_userinfo() -> None:
    """Would fail if a failure report exposed credentials embedded in a URL."""

    markdown = report_module.render_failed_daily_report(
        date(2026, 8, 5),
        "连接 https://alice:secret@audit.example/runtime 失败",
    )

    assert "alice:secret" not in markdown
    assert "audit\\.example" in markdown


def test_daily_report_replaces_extended_machine_ids_without_hiding_normal_chinese() -> None:
    """Would fail if non-trace machine IDs leaked into prose or ordinary text disappeared."""

    markdown = report_module.render_daily_codex_report(
        daily_models_module.DailyCodexAuditReport(
            audit_date=date(2026, 8, 5),
            daily_summary="普通中文保留；observation:obs-123 需要查看。",
            activity_summary="run:run-123 与 score:quality-1 已记录。",
            memory_summary="请求 1234 不应被隐藏。",
            infrastructure_summary="实例 123e4567-e89b-12d3-a456-426614174000 可追溯。",
            limitations=["证据 00000000-0000-0000-0000-000000000000 不完整。"],
        )
    )

    body, _ = markdown.split("## 证据附录", maxsplit=1)
    assert "observation:obs-123" not in body
    assert "run:run-123" not in body
    assert "score:quality-1" not in body
    assert "123e4567-e89b-12d3-a456-426614174000" not in body
    assert "00000000-0000-0000-0000-000000000000" not in body
    assert body.count("机器证据见附录") >= 5
    assert "普通中文保留" in body
    assert "请求 1234 不应被隐藏" in body


def test_daily_report_replaces_machine_ids_adjacent_to_chinese_text() -> None:
    """Would fail if Unicode word boundaries let Chinese-adjacent machine IDs leak."""

    markdown = report_module.render_daily_codex_report(
        daily_models_module.DailyCodexAuditReport(
            audit_date=date(2026, 8, 5),
            daily_summary="证据trace:abc仍需查看。",
            activity_summary="普通中文保持可读。",
            memory_summary="实例<123e4567-e89b-12d3-a456-426614174000>已记录。",
            infrastructure_summary="正常运行。",
        )
    )

    body, _ = markdown.split("## 证据附录", maxsplit=1)
    assert "trace:abc" not in body
    assert "123e4567-e89b-12d3-a456-426614174000" not in body
    assert body.count("机器证据见附录") >= 2
    assert "普通中文保持可读" in body


def test_issue_evidence_refs_reject_markdown_and_html_controls() -> None:
    """Would fail if a machine evidence ref could escape the appendix code span."""

    for field_name, value in (
        ("trace_evidence_refs", "trace:bad`ref"),
        ("trace_evidence_refs", "trace:<script>"),
        ("code_evidence_refs", "code:bad[link]"),
        ("code_evidence_refs", "test:path*wildcard"),
    ):
        with pytest.raises(ValueError, match="evidence reference"):
            DailyAuditIssue(
                issue_key="tool.email_for_market_data",
                status="open",
                title="错误使用邮件搜索",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 5),
                **{field_name: [value]},
            )


def test_issue_evidence_refs_accept_trace_commit_and_test_paths() -> None:
    """Would fail if safe existing trace, commit, and test-path evidence stopped working."""

    issue = DailyAuditIssue(
        issue_key="tool.email_for_market_data",
        status="open",
        title="错误使用邮件搜索",
        first_seen=date(2026, 8, 5),
        last_seen=date(2026, 8, 5),
        trace_evidence_refs=["trace:abc/observation:def"],
        code_evidence_refs=["code:fa1d777", "test:tests/tdd/runtime_audit/test_daily.py"],
        runtime_verification_refs=["trace:run-20260806/observation:fixed"],
    )

    assert issue.trace_evidence_refs == ["trace:abc/observation:def"]
    assert issue.code_evidence_refs[-1] == "test:tests/tdd/runtime_audit/test_daily.py"


def test_issue_requires_runtime_evidence_before_verified() -> None:
    """Would fail if code/test evidence alone could mark an issue runtime verified."""

    previous = IssueRegistry(
        issues={
            "tool.email_for_market_data": DailyAuditIssue(
                issue_key="tool.email_for_market_data",
                status="open",
                title="错误使用邮件搜索",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 5),
                trace_evidence_refs=["trace:bad"],
            )
        }
    )
    addressed = previous.issues["tool.email_for_market_data"].model_copy(
        update={
            "status": "code_addressed",
            "code_evidence_refs": [
                "code:abc123",
                "test:tests/tdd/tool/test_market.py",
            ],
        }
    )

    merged = merge_issue_registry(previous, [addressed], date(2026, 8, 6))

    assert merged.issues[addressed.issue_key].status == "code_addressed"

    invalid = addressed.model_copy(update={"status": "runtime_verified"})
    with pytest.raises(ValueError, match="runtime verification evidence"):
        merge_issue_registry(merged, [invalid], date(2026, 8, 7))

    reused_bad_trace = addressed.model_copy(
        update={
            "status": "runtime_verified",
            "runtime_verification_refs": ["trace:bad"],
        }
    )
    with pytest.raises(ValueError, match="subsequent runtime verification evidence"):
        merge_issue_registry(merged, [reused_bad_trace], date(2026, 8, 7))


def test_issue_runtime_verification_requires_a_subsequent_trace() -> None:
    """Would fail if an old bad trace could verify a later runtime fix."""

    previous = IssueRegistry(
        issues={
            "tool.email_for_market_data": DailyAuditIssue(
                issue_key="tool.email_for_market_data",
                status="code_addressed",
                title="错误使用邮件搜索",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 6),
                trace_evidence_refs=["trace:bad"],
                code_evidence_refs=["code:abc123"],
            )
        }
    )
    verified = previous.issues["tool.email_for_market_data"].model_copy(
        update={
            "status": "runtime_verified",
            "runtime_verification_refs": ["trace:fixed"],
        }
    )

    merged = merge_issue_registry(previous, [verified], date(2026, 8, 7))

    issue = merged.issues[verified.issue_key]
    assert issue.status == "runtime_verified"
    assert issue.runtime_verification_refs == ["trace:fixed"]
    assert issue.last_seen == date(2026, 8, 7)


def test_issue_regression_requires_a_new_trace_and_preserves_history() -> None:
    """Would fail if an historic bad trace could regress or be overwritten."""

    previous = IssueRegistry(
        issues={
            "tool.email_for_market_data": DailyAuditIssue(
                issue_key="tool.email_for_market_data",
                status="runtime_verified",
                title="错误使用邮件搜索",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 7),
                trace_evidence_refs=["trace:bad"],
                code_evidence_refs=["code:abc123"],
                runtime_verification_refs=["trace:fixed"],
            )
        }
    )
    invalid = previous.issues["tool.email_for_market_data"].model_copy(
        update={"status": "regressed"}
    )
    with pytest.raises(ValueError, match="new trace evidence"):
        merge_issue_registry(previous, [invalid], date(2026, 8, 8))

    regressed = invalid.model_copy(
        update={"trace_evidence_refs": ["trace:bad", "trace:bad-again"]}
    )
    merged = merge_issue_registry(previous, [regressed], date(2026, 8, 8))

    issue = merged.issues[regressed.issue_key]
    assert issue.status == "regressed"
    assert issue.trace_evidence_refs == ["trace:bad", "trace:bad-again"]
    assert issue.runtime_verification_refs == ["trace:fixed"]


def test_issue_registry_keeps_unobserved_history_and_round_trips_through_store(
    tmp_path: Path,
) -> None:
    """Would fail if an absent observation closed an issue or storage lost its registry."""

    issue = DailyAuditIssue(
        issue_key="tool.email_for_market_data",
        status="open",
        title="错误使用邮件搜索",
        first_seen=date(2026, 8, 5),
        last_seen=date(2026, 8, 5),
        trace_evidence_refs=["trace:bad"],
    )
    merged = merge_issue_registry(
        IssueRegistry(issues={issue.issue_key: issue}), [], date(2026, 8, 6)
    )
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")

    path = store.write_issue_registry(merged)

    assert path == store.issues_path
    assert store.read_issue_registry() == merged


def test_issue_identity_is_stripped_and_registry_keys_must_match() -> None:
    """Would fail if one logical issue could be addressed under two registry keys."""

    issue = DailyAuditIssue(
        issue_key="  tool.email_for_market_data  ",
        status="open",
        title="错误使用邮件搜索",
        first_seen=date(2026, 8, 5),
        last_seen=date(2026, 8, 5),
        trace_evidence_refs=["trace:bad"],
    )

    assert issue.issue_key == "tool.email_for_market_data"
    with pytest.raises(ValueError, match="issue registry key"):
        IssueRegistry(issues={"different.key": issue})
    with pytest.raises(ValueError, match="issue_key"):
        DailyAuditIssue(
            issue_key="  ",
            status="open",
            title="错误使用邮件搜索",
            first_seen=date(2026, 8, 5),
            last_seen=date(2026, 8, 5),
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("trace_evidence_refs", ["trace: "]),
        ("trace_evidence_refs", ["code:wrong-category"]),
        ("runtime_verification_refs", ["code:wrong-category"]),
        ("code_evidence_refs", ["commit:pretend-code"]),
        ("code_evidence_refs", ["  "]),
    ],
)
def test_issue_evidence_requires_a_nonblank_reference_in_its_own_category(
    field_name: str,
    value: list[str],
) -> None:
    """Would fail if malformed or cross-category evidence could pass a status guard."""

    with pytest.raises(ValueError, match="evidence reference"):
        DailyAuditIssue(
            issue_key="tool.email_for_market_data",
            status="open",
            title="错误使用邮件搜索",
            first_seen=date(2026, 8, 5),
            last_seen=date(2026, 8, 5),
            **{field_name: value},
        )


@pytest.mark.parametrize("audit_date", [date(2026, 8, 6), date(2026, 8, 5)])
def test_issue_runtime_verification_requires_a_later_audit_date(
    audit_date: date,
) -> None:
    """Would fail if a same-day or older trace could verify a fix retrospectively."""

    previous = IssueRegistry(
        issues={
            "tool.email_for_market_data": DailyAuditIssue(
                issue_key="tool.email_for_market_data",
                status="code_addressed",
                title="错误使用邮件搜索",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 6),
                trace_evidence_refs=["trace:bad"],
                code_evidence_refs=["code:abc123"],
            )
        }
    )
    verified = previous.issues["tool.email_for_market_data"].model_copy(
        update={
            "status": "runtime_verified",
            "runtime_verification_refs": ["trace:fixed"],
        }
    )

    with pytest.raises(ValueError, match="after previous last_seen"):
        merge_issue_registry(previous, [verified], audit_date)


@pytest.mark.parametrize("audit_date", [date(2026, 8, 7), date(2026, 8, 6)])
def test_issue_regression_requires_a_later_audit_date(audit_date: date) -> None:
    """Would fail if a same-day or older trace could rewrite a verified issue as regressed."""

    previous = IssueRegistry(
        issues={
            "tool.email_for_market_data": DailyAuditIssue(
                issue_key="tool.email_for_market_data",
                status="runtime_verified",
                title="错误使用邮件搜索",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 7),
                trace_evidence_refs=["trace:bad"],
                code_evidence_refs=["code:abc123"],
                runtime_verification_refs=["trace:fixed"],
            )
        }
    )
    regressed = previous.issues["tool.email_for_market_data"].model_copy(
        update={
            "status": "regressed",
            "trace_evidence_refs": ["trace:bad", "trace:bad-again"],
        }
    )

    with pytest.raises(ValueError, match="after previous last_seen"):
        merge_issue_registry(previous, [regressed], audit_date)


def test_daily_artifacts_keep_codex_json_internal_and_publish_one_markdown(tmp_path: Path) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    attempt = DailyAuditAttempt(
        attempt_id="runtime_audit_20260806_0015",
        audit_date=date(2026, 8, 5),
        status="succeeded",
        bundle_path="/tmp/bundle.json",
        codex_output_path="/tmp/codex.json",
    )
    attempt_path = store.write_attempt(attempt)
    report_path = store.write_daily_report(date(2026, 8, 5), "# 日报", replace=True)
    assert attempt_path == store.state_dir / "attempts" / f"{attempt.attempt_id}.json"
    assert store.codex_json_path(attempt.attempt_id).parent == store.state_dir / "attempts"
    assert report_path == store.reports_dir / "2026-08-05.md"
    assert list(store.reports_dir.glob("*.json")) == []


def test_failed_rerun_does_not_replace_successful_daily_report(tmp_path: Path) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    path = store.write_daily_report(date(2026, 8, 5), "成功日报", replace=True)
    store.write_failed_daily_report_if_absent(date(2026, 8, 5), "失败日报")
    assert path.read_text(encoding="utf-8").strip() == "成功日报"


def test_rolling_markdown_stays_internal_and_cli_reports_its_actual_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    bundle = SimpleNamespace(audit_run_id="runtime_audit_20260806_0015")
    rolling_path = store.write_deterministic_report(bundle, "内部报告")

    assert rolling_path == (
        store.attempts_dir / "runtime_audit_20260806_0015.deterministic.md"
    )
    assert list(store.reports_dir.glob("*.md")) == []

    monkeypatch.setattr(
        "assistant_agent.observability.runtime_audit.cli._collect",
        lambda *args, **kwargs: (Path("/tmp/bundle.json"), rolling_path, bundle),
    )

    assert main(["--no-env-file", "--repo-root", str(tmp_path), "run", "--skip-codex"]) == 0
    assert json.loads(capsys.readouterr().out)["report_path"] == str(rolling_path)


def test_concurrent_failed_publish_cannot_replace_successful_daily_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    failure_ready = threading.Event()
    allow_failed_publish = threading.Event()
    original_publish = storage_module._atomic_write_if_absent

    def blocked_failed_publish(path: Path, content: str) -> bool:
        failure_ready.set()
        assert allow_failed_publish.wait(timeout=5)
        return original_publish(path, content)

    monkeypatch.setattr(storage_module, "_atomic_write_if_absent", blocked_failed_publish)
    failed_writer = threading.Thread(
        target=store.write_failed_daily_report_if_absent,
        args=(date(2026, 8, 5), "失败日报"),
    )
    failed_writer.start()
    assert failure_ready.wait(timeout=5)

    successful_path = store.write_daily_report(date(2026, 8, 5), "成功日报", replace=True)
    allow_failed_publish.set()
    failed_writer.join(timeout=5)

    assert not failed_writer.is_alive()
    assert successful_path.read_text(encoding="utf-8").strip() == "成功日报"


def test_no_replace_publish_keeps_partial_content_off_the_final_daily_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    partial_write_started = threading.Event()
    allow_finish = threading.Event()
    original_fdopen = storage_module.os.fdopen

    class PausedWriter:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            self.handle.close()

        def write(self, content: str) -> int:
            midpoint = len(content) // 2
            self.handle.write(content[:midpoint])
            self.handle.flush()
            partial_write_started.set()
            assert allow_finish.wait(timeout=5)
            return midpoint + self.handle.write(content[midpoint:])

        def flush(self) -> None:
            self.handle.flush()

        def fileno(self) -> int:
            return self.handle.fileno()

    def controlled_fdopen(*args, **kwargs):
        handle = original_fdopen(*args, **kwargs)
        if threading.current_thread().name == "failed-writer":
            return PausedWriter(handle)
        return handle

    monkeypatch.setattr(storage_module.os, "fdopen", controlled_fdopen)
    failed_writer = threading.Thread(
        name="failed-writer",
        target=store.write_failed_daily_report_if_absent,
        args=(date(2026, 8, 5), "失败日报内容"),
    )
    failed_writer.start()
    assert partial_write_started.wait(timeout=5)

    try:
        assert not store.daily_report_path(date(2026, 8, 5)).exists()
    finally:
        allow_finish.set()
        failed_writer.join(timeout=5)

    assert not failed_writer.is_alive()
    assert store.daily_report_path(date(2026, 8, 5)).read_text(encoding="utf-8") == "失败日报内容\n"


def test_failed_no_replace_cleanup_cannot_delete_successful_daily_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    failure_ready = threading.Event()
    allow_failure = threading.Event()
    failures: list[Exception] = []
    original_fdopen = storage_module.os.fdopen

    class FailingWriter:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            self.handle.close()

        def write(self, content: str) -> int:
            failure_ready.set()
            assert allow_failure.wait(timeout=5)
            raise OSError("simulated failed daily publish")

    def controlled_fdopen(*args, **kwargs):
        handle = original_fdopen(*args, **kwargs)
        if threading.current_thread().name == "failed-writer":
            return FailingWriter(handle)
        return handle

    monkeypatch.setattr(storage_module.os, "fdopen", controlled_fdopen)

    def write_failure_report() -> None:
        try:
            store.write_failed_daily_report_if_absent(date(2026, 8, 5), "失败日报")
        except Exception as exc:
            failures.append(exc)

    failed_writer = threading.Thread(name="failed-writer", target=write_failure_report)
    failed_writer.start()
    assert failure_ready.wait(timeout=5)

    successful_path = store.write_daily_report(date(2026, 8, 5), "成功日报", replace=True)
    allow_failure.set()
    failed_writer.join(timeout=5)

    assert not failed_writer.is_alive()
    assert len(failures) == 1
    assert successful_path.read_text(encoding="utf-8") == "成功日报\n"


def test_legacy_watermark_is_not_a_completed_daily_audit(tmp_path: Path) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    store.watermark_path.parent.mkdir(parents=True)
    store.watermark_path.write_text(
        json.dumps(
            {
                "schema_version": "assistant_agent_runtime_audit_watermark_v1",
                "audit_run_id": "runtime_audit_20260806_0015",
                "last_window_end": "2026-08-05T16:00:00Z",
                "bundle_path": "/tmp/legacy-bundle.json",
            }
        ),
        encoding="utf-8",
    )

    assert store.last_completed_date() is None

    watermark_path = store.mark_day_completed(
        date(2026, 8, 5),
        attempt_id="runtime_audit_20260806_0015",
        bundle_path="/tmp/bundle.json",
    )

    assert watermark_path == store.watermark_path
    assert json.loads(watermark_path.read_text(encoding="utf-8")) == {
        "schema_version": "assistant_agent_runtime_audit_watermark_v2",
        "last_completed_date": "2026-08-05",
        "last_attempt_id": "runtime_audit_20260806_0015",
        "bundle_path": "/tmp/bundle.json",
    }
    assert store.last_completed_date() == date(2026, 8, 5)


def test_bundle_resolution_prefers_latest_pointer_then_legacy_watermark(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    store = RuntimeAuditArtifactStore(repo_root / "runtime_audit")
    store.latest_bundle_path.parent.mkdir(parents=True)
    store.latest_bundle_path.write_text(
        json.dumps({"bundle_path": "latest.json"}), encoding="utf-8"
    )
    store.watermark_path.write_text(
        json.dumps({"bundle_path": "legacy.json"}), encoding="utf-8"
    )

    assert _resolve_bundle_path(None, store=store, repo_root=repo_root) == (
        repo_root / "latest.json"
    )

    store.latest_bundle_path.unlink()

    assert _resolve_bundle_path(None, store=store, repo_root=repo_root) == (
        repo_root / "legacy.json"
    )


def test_previous_day_uses_shanghai_calendar_boundaries() -> None:
    """Would fail if daily audits derived boundaries from UTC rather than Shanghai dates."""

    window = previous_day_window(datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc))

    assert window.audit_date == date(2026, 8, 5)
    assert window.start_utc == datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    assert window.end_utc == datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def test_explicit_date_uses_shanghai_calendar_boundaries() -> None:
    """Would fail if explicit dates did not use the same Shanghai day boundaries."""

    window = window_for_date(date(2026, 8, 5))

    assert window.audit_date == date(2026, 8, 5)
    assert window.start_utc == datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    assert window.end_utc == datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def test_automatic_schedule_only_selects_yesterday() -> None:
    """Would fail if an automatic run retried older failed or missed dates."""

    assert pending_audit_dates(
        yesterday=date(2026, 8, 5), last_completed=None
    ) == [date(2026, 8, 5)]
    assert pending_audit_dates(
        yesterday=date(2026, 8, 5),
        last_completed=date(2026, 8, 2),
    ) == [date(2026, 8, 5)]
    assert pending_audit_dates(
        yesterday=date(2026, 8, 5),
        last_completed=date(2026, 8, 5),
    ) == []


def test_nonempty_daily_run_collects_invokes_codex_and_publishes_markdown(
    tmp_path: Path,
) -> None:
    """Would fail if a nonempty day skipped its isolated human audit or report."""

    calls: list[dict[str, object]] = []
    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource([_daily_trace()]),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=RuntimeAuditArtifactStore(tmp_path / "runtime_audit"),
        repo_root=tmp_path,
        codex_runner=lambda **kwargs: calls.append(kwargs) or _daily_codex_report(),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert result.status == "succeeded"
    assert len(calls) == 1
    codex_input_path = Path(calls[0]["bundle_path"])
    assert codex_input_path != result.bundle_path
    assert codex_input_path.name.endswith(".codex-input.json")
    assert json.loads(codex_input_path.read_text(encoding="utf-8"))["schema_version"] == (
        "assistant_agent_daily_codex_input_v1"
    )
    assert json.loads(result.bundle_path.read_text(encoding="utf-8"))["schema_version"] == (
        "assistant_agent_runtime_audit_bundle_v2"
    )
    assert result.report_path is not None
    assert result.report_path.name == "2026-08-05.md"
    assert result.report_path.exists()
    assert RuntimeAuditArtifactStore(tmp_path / "runtime_audit").last_completed_date() == date(2026, 8, 5)


def test_empty_daily_run_does_not_invoke_codex(tmp_path: Path) -> None:
    """Would fail if known empty days incurred an unnecessary Codex invocation."""

    calls: list[dict[str, object]] = []
    (tmp_path / "graph_trace.jsonl").write_text("", encoding="utf-8")
    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource([]),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=RuntimeAuditArtifactStore(tmp_path / "runtime_audit"),
        repo_root=tmp_path,
        codex_runner=lambda **kwargs: calls.append(kwargs) or _daily_codex_report(),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert result.status == "succeeded"
    assert calls == []
    assert result.report_path is not None
    assert "昨日无可审计对话" in result.report_path.read_text(encoding="utf-8")


def test_local_only_daily_evidence_invokes_codex_not_empty_report(tmp_path: Path) -> None:
    """Would fail if local missing-export evidence were discarded as an empty day."""

    local_trace_path = tmp_path / "graph_trace.jsonl"
    local_trace_path.write_text(
        json.dumps(
            {
                "trace_id": "local-only",
                "run_id": "run-local-only",
                "node_name": "runtime",
                "event_type": "observability",
                "canonical_event": "run.completed",
                "status": "completed",
                "created_at": "2026-08-05T12:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[object] = []
    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource([]),
        local_trace_path=local_trace_path,
        store=RuntimeAuditArtifactStore(tmp_path / "runtime_audit"),
        repo_root=tmp_path,
        codex_runner=lambda **kwargs: calls.append(kwargs) or _daily_codex_report(),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert result.status == "succeeded"
    assert len(calls) == 1


def test_langfuse_failure_publishes_failed_day_without_advancing_watermark(tmp_path: Path) -> None:
    """Would fail if an unreadable source were misreported as an empty successful day."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource(RuntimeError("Langfuse unavailable")),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store,
        repo_root=tmp_path,
        codex_runner=lambda **_: pytest.fail("Codex must not run"),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert result.status == "failed"
    assert store.last_completed_date() is None
    assert "审计未完成" in store.daily_report_path(date(2026, 8, 5)).read_text(encoding="utf-8")


def test_codex_failure_preserves_bundle_and_does_not_update_state(tmp_path: Path) -> None:
    """Would fail if an optional analysis failure advanced daily state or discarded evidence."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource([_daily_trace()]),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store,
        repo_root=tmp_path,
        codex_runner=lambda **_: (_ for _ in ()).throw(RuntimeError("Codex failed")),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert result.status == "failed"
    assert result.bundle_path.exists()
    assert store.last_completed_date() is None
    assert not store.issues_path.exists()
    assert "审计未完成" in store.daily_report_path(date(2026, 8, 5)).read_text(encoding="utf-8")


def test_automatic_run_skips_older_days_and_audits_only_yesterday(tmp_path: Path) -> None:
    """Would fail if an old failure made the daily timer replay historical dates."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    store.mark_day_completed(
        date(2026, 8, 2), attempt_id="previous", bundle_path="/tmp/previous.json"
    )
    seen_dates: list[date] = []

    def codex_runner(*, audit_date: date, **_: object) -> daily_models_module.DailyCodexAuditReport:
        seen_dates.append(audit_date)
        return _daily_codex_report(audit_date=audit_date)

    results = run_pending_daily_audits(
        yesterday=date(2026, 8, 5),
        source=FakeLangfuseSource(
            [
                _daily_trace("trace-3").model_copy(
                    update={"timestamp": datetime(2026, 8, 3, 12, tzinfo=timezone.utc)}
                ),
                _daily_trace("trace-4").model_copy(
                    update={"timestamp": datetime(2026, 8, 4, 12, tzinfo=timezone.utc)}
                ),
                _daily_trace("trace-5"),
            ]
        ),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store,
        repo_root=tmp_path,
        codex_runner=codex_runner,
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert [result.audit_date for result in results] == [date(2026, 8, 5)]
    assert seen_dates == [date(2026, 8, 5)]
    assert store.last_completed_date() == date(2026, 8, 5)


@pytest.mark.parametrize("evidence_ref", ["trace:forged", "trace:historical"])
def test_invalid_current_bundle_evidence_blocks_issue_transition(
    tmp_path: Path, evidence_ref: str
) -> None:
    """Would fail if Codex could promote issues using forged or prior-bundle trace refs."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    issue = DailyAuditIssue(
        issue_key="tool.example",
        status="uncertain",
        title="待验证问题",
        first_seen=date(2026, 8, 5),
        last_seen=date(2026, 8, 5),
        trace_evidence_refs=[evidence_ref],
    )
    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource([_daily_trace("trace-current")]),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store,
        repo_root=tmp_path,
        codex_runner=lambda **_: _daily_codex_report(issue=issue),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert result.status == "failed"
    assert store.last_completed_date() is None
    assert not store.issues_path.exists()


def test_current_bundle_evidence_allows_issue_merge(tmp_path: Path) -> None:
    """Would fail if valid current-bundle trace evidence were rejected with forged refs."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    issue = DailyAuditIssue(
        issue_key="tool.example",
        status="uncertain",
        title="待验证问题",
        first_seen=date(2026, 8, 5),
        last_seen=date(2026, 8, 5),
        trace_evidence_refs=["trace:trace-current"],
    )
    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource([_daily_trace("trace-current")]),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store,
        repo_root=tmp_path,
        codex_runner=lambda **_: _daily_codex_report(issue=issue),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert result.status == "succeeded"
    assert store.read_issue_registry().issues["tool.example"].status == "uncertain"


@pytest.mark.parametrize("verification_ref", ["trace:forged", "trace:historical"])
def test_invalid_bundle_refs_cannot_promote_runtime_verification(
    tmp_path: Path, verification_ref: str
) -> None:
    """Would fail if a false verification ref could move code-addressed state forward."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    prior = DailyAuditIssue(
        issue_key="tool.example",
        status="code_addressed",
        title="待验证问题",
        first_seen=date(2026, 8, 4),
        last_seen=date(2026, 8, 4),
        trace_evidence_refs=["trace:prior"],
        code_evidence_refs=["code:change"],
    )
    store.write_issue_registry(IssueRegistry(issues={prior.issue_key: prior}))
    candidate = DailyAuditIssue(
        issue_key=prior.issue_key,
        status="runtime_verified",
        title=prior.title,
        first_seen=date(2026, 8, 5),
        last_seen=date(2026, 8, 5),
        runtime_verification_refs=[verification_ref],
    )

    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource([_daily_trace("trace-current")]),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store,
        repo_root=tmp_path,
        codex_runner=lambda **_: _daily_codex_report(issue=candidate),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert result.status == "failed"
    assert store.read_issue_registry().issues[prior.issue_key].status == "code_addressed"


def test_current_bundle_ref_can_promote_runtime_verification(tmp_path: Path) -> None:
    """Would fail if bundle authenticity blocked a genuine later verification trace."""

    repo, commit_sha = _runtime_audit_test_repo(
        tmp_path,
        committed_at="2026-08-04T13:00:00+00:00",
    )
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    prior = DailyAuditIssue(
        issue_key="tool.example",
        status="code_addressed",
        title="待验证问题",
        first_seen=date(2026, 8, 4),
        last_seen=date(2026, 8, 4),
        trace_evidence_refs=["trace:prior"],
        code_evidence_refs=[
            f"code:{commit_sha}",
            "test:tests/tdd/runtime_audit/test_regression.py",
        ],
    )
    store.write_issue_registry(IssueRegistry(issues={prior.issue_key: prior}))
    candidate = DailyAuditIssue(
        issue_key=prior.issue_key,
        status="runtime_verified",
        title=prior.title,
        first_seen=date(2026, 8, 5),
        last_seen=date(2026, 8, 5),
        runtime_verification_refs=["trace:trace-current"],
    )

    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource([_daily_trace("trace-current")]),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store,
        repo_root=repo,
        codex_runner=lambda **_: _daily_codex_report(issue=candidate),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )

    assert result.status == "succeeded"
    assert store.read_issue_registry().issues[prior.issue_key].status == "runtime_verified"


def test_rerun_replaces_success_only_after_a_new_success(tmp_path: Path) -> None:
    """Would fail if a failed rerun overwrote a previously successful daily report."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    common = {
        "window": window_for_date(date(2026, 8, 5)),
        "source": FakeLangfuseSource([_daily_trace()]),
        "local_trace_path": tmp_path / "graph_trace.jsonl",
        "store": store,
        "repo_root": tmp_path,
        "collected_at": datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    }
    first = run_one_daily_audit(
        **common, codex_runner=lambda **_: _daily_codex_report()
    )
    assert first.status == "succeeded"
    successful_report = store.daily_report_path(date(2026, 8, 5)).read_text(encoding="utf-8")

    failed = run_one_daily_audit(
        **common,
        codex_runner=lambda **_: (_ for _ in ()).throw(RuntimeError("Codex failed")),
        commit_continuous_state=False,
    )

    assert failed.status == "failed"
    assert store.daily_report_path(date(2026, 8, 5)).read_text(encoding="utf-8") == successful_report

    replacement = _daily_codex_report().model_copy(update={"daily_summary": "刷新后的结论。"})
    refreshed = run_one_daily_audit(
        **common, codex_runner=lambda **_: replacement, commit_continuous_state=False
    )

    assert refreshed.status == "succeeded"
    assert "刷新后的结论。" in store.daily_report_path(date(2026, 8, 5)).read_text(encoding="utf-8")


@pytest.mark.parametrize("audit_date", [date(2026, 8, 4), date(2026, 8, 6)])
def test_explicit_rerun_does_not_mutate_continuous_issue_or_watermark(
    tmp_path: Path, audit_date: date
) -> None:
    """Would fail if a manual historical or future refresh rewrote continuous state."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    prior = DailyAuditIssue(
        issue_key="tool.example",
        status="open",
        title="原问题",
        first_seen=date(2026, 8, 3),
        last_seen=date(2026, 8, 3),
    )
    store.write_issue_registry(IssueRegistry(issues={prior.issue_key: prior}))
    store.mark_day_completed(date(2026, 8, 5), attempt_id="continuous", bundle_path="/tmp/a")
    report = _daily_codex_report(
        audit_date=audit_date,
        issue=DailyAuditIssue(
            issue_key="tool.new", status="uncertain", title="手动问题",
            first_seen=audit_date, last_seen=audit_date, trace_evidence_refs=["trace:trace-current"],
        ),
    )

    result = run_one_daily_audit(
        window=window_for_date(audit_date),
        source=FakeLangfuseSource([_daily_trace("trace-current").model_copy(update={"timestamp": datetime(audit_date.year, audit_date.month, audit_date.day, 12, tzinfo=timezone.utc)})]),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store, repo_root=tmp_path, codex_runner=lambda **_: report,
        collected_at=datetime(2026, 8, 7, 0, 15, tzinfo=timezone.utc),
        commit_continuous_state=False,
    )

    assert result.status == "succeeded"
    assert store.last_completed_date() == date(2026, 8, 5)
    assert set(store.read_issue_registry().issues) == {"tool.example"}


@pytest.mark.parametrize(
    (
        "audit_date",
        "previous_last_seen",
        "previous_status",
        "candidate_status",
        "evidence_field",
        "committed_at",
    ),
    [
        (
            date(2026, 8, 5),
            date(2026, 8, 4),
            "code_addressed",
            "runtime_verified",
            "runtime_verification_refs",
            "2026-08-04T13:00:00+00:00",
        ),
        (
            date(2026, 8, 4),
            date(2026, 8, 3),
            "runtime_verified",
            "regressed",
            "trace_evidence_refs",
            "2026-08-03T13:00:00+00:00",
        ),
    ],
)
def test_explicit_rerun_skips_lifecycle_merge_but_keeps_bundle_evidence_gate(
    tmp_path: Path,
    audit_date: date,
    previous_last_seen: date,
    previous_status: str,
    candidate_status: str,
    evidence_field: str,
    committed_at: str,
) -> None:
    """Would fail if an explicit rerun merged lifecycle state or skipped evidence checks."""

    repo, commit_sha = _runtime_audit_test_repo(
        tmp_path,
        committed_at=committed_at,
    )
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    previous = DailyAuditIssue(
        issue_key="tool.lifecycle",
        status=previous_status,
        title="已有问题",
        first_seen=date(2026, 8, 1),
        last_seen=previous_last_seen,
        trace_evidence_refs=["trace:historical"],
        code_evidence_refs=[
            f"code:{commit_sha}",
            "test:tests/tdd/runtime_audit/test_regression.py",
        ],
        runtime_verification_refs=(
            ["trace:verified"] if previous_status == "runtime_verified" else []
        ),
    )
    store.write_issue_registry(IssueRegistry(issues={previous.issue_key: previous}))
    store.mark_day_completed(
        date(2026, 8, 5), attempt_id="continuous", bundle_path="/tmp/continuous"
    )
    registry_before = store.issues_path.read_bytes()
    watermark_before = store.watermark_path.read_bytes()

    candidate = DailyAuditIssue(
        issue_key=previous.issue_key,
        status=candidate_status,
        title=previous.title,
        first_seen=audit_date,
        last_seen=audit_date,
        **{evidence_field: ["trace:trace-current"]},
    )
    result = run_one_daily_audit(
        window=window_for_date(audit_date),
        source=FakeLangfuseSource(
            [
                _daily_trace("trace-current").model_copy(
                    update={
                        "timestamp": datetime(
                            audit_date.year,
                            audit_date.month,
                            audit_date.day,
                            12,
                            tzinfo=timezone.utc,
                        )
                    }
                )
            ]
        ),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store,
        repo_root=repo,
        codex_runner=lambda **_: _daily_codex_report(
            audit_date=audit_date, issue=candidate
        ),
        collected_at=datetime(2026, 8, 7, 0, 15, tzinfo=timezone.utc),
        commit_continuous_state=False,
    )

    assert result.status == "succeeded"
    assert store.issues_path.read_bytes() == registry_before
    assert store.watermark_path.read_bytes() == watermark_before

    forged = candidate.model_copy(
        update={evidence_field: ["trace:not-in-current-bundle"]}
    )
    rejected = run_one_daily_audit(
        window=window_for_date(audit_date),
        source=FakeLangfuseSource(
            [
                _daily_trace("trace-current").model_copy(
                    update={
                        "timestamp": datetime(
                            audit_date.year,
                            audit_date.month,
                            audit_date.day,
                            12,
                            tzinfo=timezone.utc,
                        )
                    }
                )
            ]
        ),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=store,
        repo_root=repo,
        codex_runner=lambda **_: _daily_codex_report(
            audit_date=audit_date, issue=forged
        ),
        collected_at=datetime(2026, 8, 7, 0, 16, tzinfo=timezone.utc),
        commit_continuous_state=False,
    )

    assert rejected.status == "failed"
    assert store.issues_path.read_bytes() == registry_before
    assert store.watermark_path.read_bytes() == watermark_before


@pytest.mark.parametrize("stage", ["report", "registry", "attempt", "watermark"])
def test_interrupted_continuous_commit_recovers_from_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Would fail if a partial multi-file success left daily state permanently blocked."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    original = getattr(
        store,
        {
            "report": "write_daily_report",
            "registry": "write_issue_registry",
            "attempt": "write_attempt",
            "watermark": "mark_day_completed",
        }[stage],
    )
    calls = {"value": 0}
    fail_on_call = 2 if stage == "attempt" else 1

    def interrupt_once(*args: object, **kwargs: object):
        calls["value"] += 1
        if calls["value"] == fail_on_call:
            raise OSError(f"injected {stage} failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, original.__name__, interrupt_once)
    with pytest.raises(OSError):
        run_one_daily_audit(
            window=window_for_date(date(2026, 8, 5)), source=FakeLangfuseSource([_daily_trace()]),
            local_trace_path=tmp_path / "graph_trace.jsonl", store=store, repo_root=tmp_path,
            codex_runner=lambda **_: _daily_codex_report(issue=DailyAuditIssue(
                issue_key="tool.interrupted", status="uncertain", title="中断",
                first_seen=date(2026, 8, 5), last_seen=date(2026, 8, 5),
                trace_evidence_refs=["trace:trace-current"],
            )),
            collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
        )

    intent = store.read_commit_intents()[0]
    expected_markdown = intent["markdown"]
    expected_registry = IssueRegistry.model_validate(intent["registry"])
    expected_digest = intent["desired_registry_digest"]

    recover_pending_daily_commits(store)

    report_path = store.daily_report_path(date(2026, 8, 5))
    assert report_path.read_text(encoding="utf-8").strip() == expected_markdown.strip()
    assert store.read_issue_registry() == expected_registry
    assert store.issue_registry_digest() == expected_digest
    recovered_attempt = DailyAuditAttempt.model_validate_json(
        (store.attempts_dir / f"{intent['attempt']['attempt_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert recovered_attempt.status == "succeeded"
    assert recovered_attempt.error_summary is None
    assert recovered_attempt.bundle_path == intent["attempt"]["bundle_path"]
    watermark = store.read_daily_watermark()
    assert watermark is not None
    assert watermark.last_completed_date == date(2026, 8, 5)
    assert watermark.last_attempt_id == recovered_attempt.attempt_id
    assert watermark.bundle_path == recovered_attempt.bundle_path
    assert store.read_commit_intents() == []


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "legacy",
        "unknown",
        "missing_schema",
        "non_object",
        "type_error",
        "stale",
        "watermark_conflict",
        "digest_conflict",
    ],
)
def test_invalid_commit_intent_is_atomically_quarantined_and_does_not_reblock(
    tmp_path: Path, invalid_kind: str
) -> None:
    """Would fail if malformed or conflicting journals were deleted or retried forever."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    attempt = DailyAuditAttempt(
        attempt_id=f"intent-{invalid_kind}",
        audit_date=date(2026, 8, 5),
        status="running",
        bundle_path="/tmp/bundle.json",
        codex_output_path="/tmp/codex.json",
    )
    desired_registry = IssueRegistry(
        issues={
            "tool.desired": DailyAuditIssue(
                issue_key="tool.desired",
                status="uncertain",
                title="待确认",
                first_seen=date(2026, 8, 5),
                last_seen=date(2026, 8, 5),
                trace_evidence_refs=["trace:current"],
            )
        }
    )
    intent_path = store.write_commit_intent(
        attempt,
        markdown="# 预期日报",
        registry=desired_registry,
        commit_continuous_state=True,
    )
    payload = json.loads(intent_path.read_text(encoding="utf-8"))
    if invalid_kind == "legacy":
        payload["schema_version"] = "assistant_agent_daily_commit_intent_v1"
    elif invalid_kind == "unknown":
        payload["schema_version"] = "assistant_agent_daily_commit_intent_v999"
    elif invalid_kind == "missing_schema":
        payload.pop("schema_version")
    elif invalid_kind == "non_object":
        payload = ["not-an-object"]
    elif invalid_kind == "type_error":
        payload["commit_continuous_state"] = "true"
    elif invalid_kind == "stale":
        store.mark_day_completed(
            date(2026, 8, 6), attempt_id="newer", bundle_path="/tmp/newer"
        )
    elif invalid_kind == "watermark_conflict":
        store.mark_day_completed(
            date(2026, 8, 5), attempt_id="other", bundle_path="/tmp/other"
        )
    elif invalid_kind == "digest_conflict":
        store.write_issue_registry(
            IssueRegistry(
                issues={
                    "tool.other": DailyAuditIssue(
                        issue_key="tool.other",
                        status="open",
                        title="并发状态",
                        first_seen=date(2026, 8, 5),
                        last_seen=date(2026, 8, 5),
                    )
                }
            )
        )
    raw_intent = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    intent_path.write_text(raw_intent, encoding="utf-8")
    watermark_before = (
        store.watermark_path.read_bytes() if store.watermark_path.exists() else None
    )
    registry_before = (
        store.issues_path.read_bytes() if store.issues_path.exists() else None
    )

    recover_pending_daily_commits(store)

    quarantined = list(store.quarantine_dir.glob(f"{intent_path.stem}*.json"))
    assert not intent_path.exists()
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == raw_intent
    assert not store.daily_report_path(date(2026, 8, 5)).exists()
    assert not (store.attempts_dir / f"{attempt.attempt_id}.json").exists()
    assert (
        store.watermark_path.read_bytes() if store.watermark_path.exists() else None
    ) == watermark_before
    assert (store.issues_path.read_bytes() if store.issues_path.exists() else None) == registry_before

    recover_pending_daily_commits(store)
    assert list(store.quarantine_dir.glob(f"{intent_path.stem}*.json")) == quarantined


def test_quarantine_preserves_existing_same_named_artifact(tmp_path: Path) -> None:
    """Would fail if quarantining a repeated bad journal overwrote prior evidence."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    path = store.commits_dir / "repeated.json"
    path.parent.mkdir(parents=True)
    raw_intent = '{"schema_version":"unknown"}\n'
    path.write_text(raw_intent, encoding="utf-8")
    store.quarantine_dir.mkdir(parents=True)
    existing = store.quarantine_dir / path.name
    existing.write_text("prior evidence\n", encoding="utf-8")

    recover_pending_daily_commits(store)

    assert existing.read_text(encoding="utf-8") == "prior evidence\n"
    alternatives = [item for item in store.quarantine_dir.glob("repeated*.json") if item != existing]
    assert len(alternatives) == 1
    assert alternatives[0].read_text(encoding="utf-8") == raw_intent


def test_daily_claim_serializes_concurrent_registry_updates(tmp_path: Path) -> None:
    """Would fail if concurrent adjacent daily runs lost an issue-registry update."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    first_entered = threading.Event()
    release_first = threading.Event()
    results: list[object] = []

    def runner(*, audit_date: date, **_: object) -> daily_models_module.DailyCodexAuditReport:
        if not first_entered.is_set():
            first_entered.set()
            assert release_first.wait(timeout=30)
        issue = DailyAuditIssue(
            issue_key="tool.shared", status="uncertain", title="共享问题",
            first_seen=audit_date, last_seen=audit_date, trace_evidence_refs=["trace:trace-current"],
        )
        return _daily_codex_report(audit_date=audit_date, issue=issue)

    def invoke(audit_date: date) -> None:
        results.append(run_one_daily_audit(
            window=window_for_date(audit_date),
            source=FakeLangfuseSource(
                [
                    _daily_trace().model_copy(
                        update={
                            "timestamp": datetime(
                                audit_date.year,
                                audit_date.month,
                                audit_date.day,
                                12,
                                tzinfo=timezone.utc,
                            )
                        }
                    )
                ]
            ),
            local_trace_path=tmp_path / "graph_trace.jsonl", store=store, repo_root=tmp_path,
            codex_runner=runner, collected_at=datetime(2026, 8, 7, 0, 15, tzinfo=timezone.utc),
        ))

    first = threading.Thread(target=invoke, args=(date(2026, 8, 5),))
    second = threading.Thread(target=invoke, args=(date(2026, 8, 6),))
    first.start()
    assert first_entered.wait(timeout=30)
    second.start()
    release_first.set()
    first.join(timeout=30)
    second.join(timeout=30)

    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2
    assert [result.audit_date for result in results] == [
        date(2026, 8, 5),
        date(2026, 8, 6),
    ]
    assert store.read_issue_registry().issues["tool.shared"].status == "uncertain"


def test_cli_source_factory_failure_publishes_daily_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would fail if source construction escaped as stderr-only CLI failure."""

    monkeypatch.setattr(
        "assistant_agent.observability.runtime_audit.cli.create_langfuse_audit_source_from_env",
        lambda _: (_ for _ in ()).throw(RuntimeError("source unavailable")),
    )

    assert main([
        "--no-env-file", "--repo-root", str(tmp_path), "run", "--date", "2026-08-05",
    ]) == 2

    payload = json.loads(capsys.readouterr().out)
    store = RuntimeAuditArtifactStore(tmp_path / ".data/runtime_audit")
    assert payload["failed_date"] == "2026-08-05"
    assert "审计未完成" in store.daily_report_path(date(2026, 8, 5)).read_text(encoding="utf-8")


def test_auto_source_failure_recomputes_latest_pending_date_under_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Would fail if source failure reported the stale target chosen before interleaving."""

    store = RuntimeAuditArtifactStore(tmp_path / ".data/runtime_audit")
    store.mark_day_completed(
        date(2026, 8, 2), attempt_id="prior", bundle_path="/tmp/prior"
    )
    source_creation_started = threading.Event()
    competing_run_completed = threading.Event()

    def complete_first_pending() -> None:
        assert source_creation_started.wait(timeout=5)
        with store.daily_claim():
            store.mark_day_completed(
                date(2026, 8, 3),
                attempt_id="competing",
                bundle_path="/tmp/competing",
            )
        competing_run_completed.set()

    competitor = threading.Thread(target=complete_first_pending)
    competitor.start()

    def fail_source_factory(_: object) -> object:
        source_creation_started.set()
        assert competing_run_completed.wait(timeout=5)
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(cli_module, "previous_day_window", lambda _: window_for_date(date(2026, 8, 5)))
    monkeypatch.setattr(cli_module, "create_langfuse_audit_source_from_env", fail_source_factory)

    exit_code = main(["--no-env-file", "--repo-root", str(tmp_path), "run"])
    competitor.join(timeout=5)

    assert not competitor.is_alive()
    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["audit_dates"] == ["2026-08-05"]
    assert payload["failed_date"] == "2026-08-05"
    attempts = [
        DailyAuditAttempt.model_validate_json(path.read_text(encoding="utf-8"))
        for path in store.attempts_dir.glob("*.json")
    ]
    assert [(attempt.audit_date, attempt.status) for attempt in attempts] == [
        (date(2026, 8, 5), "failed")
    ]


def test_auto_source_failure_after_competing_completion_returns_success_without_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Would fail if an already-completed automatic target were reported as failed."""

    store = RuntimeAuditArtifactStore(tmp_path / ".data/runtime_audit")
    store.mark_day_completed(
        date(2026, 8, 4), attempt_id="prior", bundle_path="/tmp/prior"
    )
    source_creation_started = threading.Event()
    competing_run_completed = threading.Event()

    def complete_only_pending() -> None:
        assert source_creation_started.wait(timeout=5)
        with store.daily_claim():
            store.mark_day_completed(
                date(2026, 8, 5),
                attempt_id="competing",
                bundle_path="/tmp/competing",
            )
        competing_run_completed.set()

    competitor = threading.Thread(target=complete_only_pending)
    competitor.start()

    def fail_source_factory(_: object) -> object:
        source_creation_started.set()
        assert competing_run_completed.wait(timeout=5)
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(cli_module, "previous_day_window", lambda _: window_for_date(date(2026, 8, 5)))
    monkeypatch.setattr(cli_module, "create_langfuse_audit_source_from_env", fail_source_factory)

    exit_code = main(["--no-env-file", "--repo-root", str(tmp_path), "run"])
    competitor.join(timeout=5)

    assert not competitor.is_alive()
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "audit_dates": [],
        "report_paths": [],
        "failed_date": None,
    }
    assert list(store.attempts_dir.glob("*.json")) == []


def test_automatic_run_recovers_yesterday_commit_without_replaying_older_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Would fail if journal recovery caused historical replay or duplicate auditing."""

    day_two = date(2026, 8, 2)
    day_three = date(2026, 8, 3)
    day_four = date(2026, 8, 4)
    store = RuntimeAuditArtifactStore(tmp_path / ".data/runtime_audit")
    store.mark_day_completed(
        day_two, attempt_id="day-two", bundle_path="/tmp/day-two"
    )
    source_windows: list[tuple[datetime, datetime]] = []
    codex_dates: list[date] = []

    class TrackingSource(FakeLangfuseSource):
        def list_traces(
            self, *, window_start: datetime, window_end: datetime
        ) -> list[LangfuseTraceSnapshot]:
            source_windows.append((window_start, window_end))
            return super().list_traces(
                window_start=window_start, window_end=window_end
            )

        def close(self) -> None:
            return None

    traces = [
        _daily_trace("trace-day-three").model_copy(
            update={"timestamp": datetime(2026, 8, 3, 12, tzinfo=timezone.utc)}
        ),
        _daily_trace("trace-day-four").model_copy(
            update={"timestamp": datetime(2026, 8, 4, 12, tzinfo=timezone.utc)}
        ),
    ]
    monkeypatch.setattr(
        cli_module,
        "previous_day_window",
        lambda _: window_for_date(day_four),
    )
    monkeypatch.setattr(
        cli_module,
        "create_langfuse_audit_source_from_env",
        lambda _: TrackingSource(traces),
    )

    def fake_codex_runner(
        *, audit_date: date, **_: object
    ) -> daily_models_module.DailyCodexAuditReport:
        codex_dates.append(audit_date)
        return _daily_codex_report(audit_date=audit_date)

    monkeypatch.setattr(cli_module, "run_daily_codex_report", fake_codex_runner)
    original_clear = RuntimeAuditArtifactStore.clear_commit_intent
    clear_calls = {"value": 0}

    def fail_first_clear(
        current_store: RuntimeAuditArtifactStore, attempt_id: str
    ) -> None:
        clear_calls["value"] += 1
        if clear_calls["value"] == 1:
            raise OSError("injected journal clear failure")
        original_clear(current_store, attempt_id)

    monkeypatch.setattr(
        RuntimeAuditArtifactStore, "clear_commit_intent", fail_first_clear
    )

    first_exit = main(["--no-env-file", "--repo-root", str(tmp_path), "run"])
    first_payload = json.loads(capsys.readouterr().out)

    assert first_exit == 2
    assert first_payload["audit_dates"] == [day_four.isoformat()]
    assert first_payload["failed_date"] == day_four.isoformat()
    assert codex_dates == [day_four]
    assert source_windows == [
        (
            window_for_date(day_four).start_utc,
            window_for_date(day_four).end_utc,
        )
    ]
    assert not store.daily_report_path(day_three).exists()
    assert store.daily_report_path(day_four).exists()
    attempts_after_first = [
        DailyAuditAttempt.model_validate_json(path.read_text(encoding="utf-8"))
        for path in store.attempts_dir.glob("*.json")
    ]
    assert {attempt.audit_date for attempt in attempts_after_first} == {day_four}
    assert [intent["attempt"]["audit_date"] for intent in store.read_commit_intents()] == [
        day_four.isoformat()
    ]

    second_exit = main(["--no-env-file", "--repo-root", str(tmp_path), "run"])
    second_payload = json.loads(capsys.readouterr().out)

    assert second_exit == 0
    assert second_payload["audit_dates"] == []
    assert second_payload["failed_date"] is None
    assert codex_dates == [day_four]
    assert store.daily_report_path(day_four).exists()
    assert store.last_completed_date() == day_four
    assert store.read_commit_intents() == []


def test_auto_run_with_no_pending_day_does_not_create_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Would fail if a completed automatic schedule still initialized Langfuse."""

    store = RuntimeAuditArtifactStore(tmp_path / ".data/runtime_audit")
    store.mark_day_completed(
        date(2026, 8, 5), attempt_id="complete", bundle_path="/tmp/complete"
    )
    monkeypatch.setattr(cli_module, "previous_day_window", lambda _: window_for_date(date(2026, 8, 5)))
    monkeypatch.setattr(
        cli_module,
        "create_langfuse_audit_source_from_env",
        lambda _: pytest.fail("source must not be created when no day is pending"),
    )

    assert main(["--no-env-file", "--repo-root", str(tmp_path), "run"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "audit_dates": [],
        "report_paths": [],
        "failed_date": None,
    }


def test_source_close_warning_keeps_success_exit_code_and_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Would fail if best-effort source cleanup overturned a committed daily result."""

    local_trace_path = tmp_path / ".data/graph_trace.jsonl"
    local_trace_path.parent.mkdir(parents=True)
    local_trace_path.write_text("", encoding="utf-8")

    class CloseWarningSource(FakeLangfuseSource):
        def close(self) -> None:
            raise RuntimeError("close warning")

    monkeypatch.setattr(
        cli_module,
        "create_langfuse_audit_source_from_env",
        lambda _: CloseWarningSource([]),
    )

    exit_code = main(
        [
            "--no-env-file",
            "--repo-root",
            str(tmp_path),
            "run",
            "--date",
            "2026-08-05",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["audit_dates"] == ["2026-08-05"]
    assert payload["failed_date"] is None
    warning = json.loads(captured.err)
    assert warning["status"] == "source_close_warning"


def test_rolling_and_daily_entrypoints_share_claim_without_self_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if legacy rolling collection crossed or recursively deadlocked daily state."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    daily_entered = threading.Event()
    release_daily = threading.Event()
    rolling_invoked = threading.Event()
    rolling_claim_attempted = threading.Event()
    rolling_source_created = threading.Event()
    errors: list[BaseException] = []

    def daily_codex(**_: object) -> daily_models_module.DailyCodexAuditReport:
        daily_entered.set()
        assert release_daily.wait(timeout=30)
        return _daily_codex_report()

    class RollingSource(FakeLangfuseSource):
        def close(self) -> None:
            return None

    def create_rolling_source(_: object) -> RollingSource:
        rolling_source_created.set()
        return RollingSource([])

    monkeypatch.setattr(cli_module, "create_langfuse_audit_source_from_env", create_rolling_source)
    original_claim = store.daily_claim

    @contextmanager
    def observed_claim():
        if threading.current_thread().name == "rolling-audit":
            rolling_claim_attempted.set()
        with original_claim():
            yield

    monkeypatch.setattr(store, "daily_claim", observed_claim)

    def run_daily() -> None:
        try:
            run_one_daily_audit(
                window=window_for_date(date(2026, 8, 5)),
                source=FakeLangfuseSource([_daily_trace()]),
                local_trace_path=tmp_path / "graph_trace.jsonl",
                store=store,
                repo_root=tmp_path,
                codex_runner=daily_codex,
                collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
            )
        except BaseException as exc:
            errors.append(exc)

    rolling_args = SimpleNamespace(
        window_hours=2.0,
        date=None,
        local_trace_path=Path("graph_trace.jsonl"),
        judge_grace_minutes=15.0,
        low_score_threshold=0.5,
    )

    def run_rolling() -> None:
        rolling_invoked.set()
        try:
            cli_module._collect(rolling_args, repo_root=tmp_path, store=store)
        except BaseException as exc:
            errors.append(exc)

    daily_thread = threading.Thread(name="daily-audit", target=run_daily)
    rolling_thread = threading.Thread(name="rolling-audit", target=run_rolling)
    daily_thread.start()
    assert daily_entered.wait(timeout=30)
    rolling_thread.start()
    assert rolling_invoked.wait(timeout=30)
    assert rolling_claim_attempted.wait(timeout=30)
    assert not rolling_source_created.is_set()

    release_daily.set()
    daily_thread.join(timeout=30)
    rolling_thread.join(timeout=30)

    assert not daily_thread.is_alive()
    assert not rolling_thread.is_alive()
    assert rolling_source_created.is_set()
    assert errors == []


@pytest.mark.parametrize("target_date", [date(2026, 8, 4), date(2026, 8, 5)])
def test_non_newer_continuous_run_rejects_before_all_daily_side_effects(
    tmp_path: Path, target_date: date
) -> None:
    """Would fail if a backward or same-day run rewrote the latest checkpoint."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    store.mark_day_completed(date(2026, 8, 5), attempt_id="prior", bundle_path="/tmp/prior")
    with store.daily_claim():
        pass
    before = {
        path.relative_to(store.root): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    source_calls: list[object] = []
    codex_calls: list[object] = []

    class TrackingSource(FakeLangfuseSource):
        def list_traces(self, **kwargs: datetime) -> list[LangfuseTraceSnapshot]:
            source_calls.append(kwargs)
            return super().list_traces(**kwargs)

    with pytest.raises(ValueError, match="target must be newer"):
        run_one_daily_audit(
            window=window_for_date(target_date),
            source=TrackingSource(
                [
                    _daily_trace().model_copy(
                        update={
                            "timestamp": datetime(
                                target_date.year,
                                target_date.month,
                                target_date.day,
                                12,
                                tzinfo=timezone.utc,
                            )
                        }
                    )
                ]
            ),
            local_trace_path=tmp_path / "graph_trace.jsonl", store=store, repo_root=tmp_path,
            codex_runner=lambda **kwargs: codex_calls.append(kwargs)
            or _daily_codex_report(audit_date=target_date),
            collected_at=datetime(2026, 8, 8, 0, 15, tzinfo=timezone.utc),
        )

    after = {
        path.relative_to(store.root): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    assert source_calls == []
    assert codex_calls == []
    assert after == before


def test_daily_run_dry_run_lists_only_the_requested_date(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Would fail if dry-run contacted a source instead of exposing planned daily paths."""

    monkeypatch.setattr(
        cli_module,
        "create_langfuse_audit_source_from_env",
        lambda _: pytest.fail("daily dry-run must not create a Langfuse source"),
    )
    monkeypatch.setattr(
        cli_module,
        "run_daily_codex_report",
        lambda **_: pytest.fail("daily dry-run must not invoke Codex"),
    )

    assert main(
        [
            "--no-env-file",
            "--repo-root",
            str(tmp_path),
            "run",
            "--date",
            "2026-08-05",
            "--dry-run",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["audit_dates"] == ["2026-08-05"]
    assert payload["report_paths"] == [
        str(tmp_path / ".data/runtime_audit/reports/2026-08-05.md")
    ]
    assert payload["failed_date"] is None
    assert payload["codex_enabled"] is True
    assert payload["production_mutation_allowed"] is False


def test_run_defaults_to_previous_calendar_day_and_date_conflicts_with_window_hours() -> None:
    """Would fail if run retained a rolling default or accepted competing window choices."""

    parser = _parser()
    args = parser.parse_args(["run"])

    assert args.date is None
    assert args.window_hours is None
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--date", "2026-08-05", "--window-hours", "2"])


def test_collection_excludes_window_end_from_remote_and_local_evidence(tmp_path: Path) -> None:
    """Would fail if the midnight record were counted in both adjacent daily audits."""

    window_start = datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=1)
    local_trace_path = tmp_path / "graph_trace.jsonl"
    local_trace_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "trace_id": trace_id,
                    "run_id": f"run-{trace_id}",
                    "node_name": "runtime",
                    "event_type": "observability",
                    "canonical_event": "run.completed",
                    "status": "completed",
                    "created_at": created_at.isoformat(),
                }
            )
            for trace_id, created_at in (
                ("local-start", window_start),
                ("local-end", window_end),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    class Source:
        def list_traces(self, **_: datetime) -> list[LangfuseTraceSnapshot]:
            return [
                LangfuseTraceSnapshot(
                    trace_id="remote-start",
                    name="assistant.turn",
                    timestamp=window_start,
                    observations=[],
                    scores=[],
                ),
                LangfuseTraceSnapshot(
                    trace_id="remote-end",
                    name="assistant.turn",
                    timestamp=window_end,
                    observations=[],
                    scores=[],
                ),
            ]

    bundle = collect_runtime_audit(
        source=Source(),
        local_trace_path=local_trace_path,
        window_start=window_start,
        window_end=window_end,
        collected_at=window_end,
    )

    assert [trace.trace_id for trace in bundle.traces] == ["remote-start"]
    assert [manifest.trace_id for manifest in bundle.local_manifests] == ["local-start"]


def test_langfuse_adapter_excludes_fetched_trace_at_window_end() -> None:
    """Would fail if an inclusive Langfuse query leaked the next day's first trace."""

    window_start = datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=1)

    class TraceApi:
        def list(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                data=[SimpleNamespace(id="trace-start"), SimpleNamespace(id="trace-end")],
                meta=SimpleNamespace(total_pages=1),
            )

        def get(self, trace_id: str) -> dict[str, object]:
            timestamp = window_start if trace_id == "trace-start" else window_end
            return {
                "id": trace_id,
                "name": "assistant.turn",
                "timestamp": timestamp,
                "observations": [],
                "scores": [],
            }

    class ScoresApi:
        def get_many_v3(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(data=[], meta=SimpleNamespace(cursor=None))

    source = LangfuseSdkAuditSource(
        SimpleNamespace(api=SimpleNamespace(trace=TraceApi(), scores_v3=ScoresApi()))
    )

    traces = source.list_traces(window_start=window_start, window_end=window_end)

    assert [trace.trace_id for trace in traces] == ["trace-start"]

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from assistant_agent.observability.runtime_audit import runner as runner_module
from assistant_agent.observability.runtime_audit.cli import main
from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditAttempt,
    DailyAuditIssue,
    DailyCodexAuditReport,
    IssueRegistry,
)
from assistant_agent.observability.runtime_audit.daily_runner import run_one_daily_audit
from assistant_agent.observability.runtime_audit.daily_window import window_for_date
from assistant_agent.observability.runtime_audit.issues import merge_issue_registry
from assistant_agent.observability.runtime_audit.models import (
    LangfuseScoreSnapshot,
    LangfuseTraceSnapshot,
)
from assistant_agent.observability.runtime_audit.report import render_daily_codex_report
from assistant_agent.observability.runtime_audit.storage import RuntimeAuditArtifactStore


AUDIT_DATE = date(2026, 8, 5)
COLLECTED_AT = datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc)


class FakeSource:
    def __init__(self, traces: list[LangfuseTraceSnapshot]) -> None:
        self.traces = traces

    def list_traces(self, **_: datetime) -> list[LangfuseTraceSnapshot]:
        return self.traces


def _trace(
    trace_id: str = "trace-current",
    *,
    timestamp: datetime = datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
    input: object = None,
    scores: list[LangfuseScoreSnapshot] | None = None,
) -> LangfuseTraceSnapshot:
    return LangfuseTraceSnapshot(
        trace_id=trace_id,
        name="assistant.turn",
        timestamp=timestamp,
        input=input,
        observations=[],
        scores=scores or [],
    )


def _report(
    *,
    issue: DailyAuditIssue | None = None,
    daily_summary: str = "昨日有一条可审计对话。",
) -> DailyCodexAuditReport:
    return DailyCodexAuditReport(
        audit_date=AUDIT_DATE,
        daily_summary=daily_summary,
        activity_summary="昨日完成一条对话。",
        issues=[] if issue is None else [issue],
        memory_summary="没有记忆问题。",
        infrastructure_summary="远端证据可读。",
    )


def _run(
    tmp_path: Path,
    *,
    source: FakeSource,
    codex_runner,
    local_trace_path: Path | None = None,
    store: RuntimeAuditArtifactStore | None = None,
    repo_root: Path | None = None,
    commit_continuous_state: bool = True,
):
    return run_one_daily_audit(
        window=window_for_date(AUDIT_DATE),
        source=source,
        local_trace_path=local_trace_path or tmp_path / "graph_trace.jsonl",
        store=store or RuntimeAuditArtifactStore(tmp_path / "runtime_audit"),
        repo_root=repo_root or tmp_path,
        codex_runner=codex_runner,
        collected_at=COLLECTED_AT,
        commit_continuous_state=commit_continuous_state,
    )


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _committed_test_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    committed_at: str = "2026-08-05T13:00:00+00:00",
) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    test_path = repo / "tests/tdd/example/test_regression.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_regression():\n    assert True\n", encoding="utf-8")
    _git(repo.parent, "init", str(repo))
    _git(repo, "config", "user.email", "audit@example.invalid")
    _git(repo, "config", "user.name", "Runtime Audit Test")
    _git(repo, "add", "tests/tdd/example/test_regression.py")
    environment = dict(__import__("os").environ)
    environment.update(
        {
            "GIT_AUTHOR_DATE": committed_at,
            "GIT_COMMITTER_DATE": committed_at,
        }
    )
    _git(repo, "commit", "-m", "fix: add regression coverage", env=environment)
    return repo, _git(repo, "rev-parse", "HEAD")


def test_remote_trace_survives_unreadable_local_source_and_reports_limitation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if a local OSError aborted a complete remote audit or hid its limitation."""

    local_path = tmp_path / "graph_trace.jsonl"
    local_path.write_text("placeholder\n", encoding="utf-8")
    original_open = Path.open

    def fail_local_open(self: Path, *args: object, **kwargs: object):
        if self == local_path:
            raise OSError("https://reader:secret@local.invalid denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_local_open)
    calls: list[object] = []
    result = _run(
        tmp_path,
        source=FakeSource([_trace()]),
        local_trace_path=local_path,
        codex_runner=lambda **kwargs: calls.append(kwargs) or _report(),
    )

    assert result.status == "succeeded"
    assert len(calls) == 1
    markdown = result.report_path.read_text(encoding="utf-8")
    assert "本地完整性证据不可用" in markdown
    assert "reader:secret" not in markdown


@pytest.mark.parametrize("local_content", ["not-json\n", "{also-not-json}\n"])
def test_invalid_only_local_source_cannot_certify_an_empty_day(
    tmp_path: Path, local_content: str
) -> None:
    """Would fail if invalid-only local JSONL were treated as a readable empty source."""

    local_path = tmp_path / "graph_trace.jsonl"
    local_path.write_text(local_content, encoding="utf-8")
    calls: list[object] = []
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    result = _run(
        tmp_path,
        source=FakeSource([]),
        local_trace_path=local_path,
        store=store,
        codex_runner=lambda **kwargs: calls.append(kwargs) or _report(),
    )

    assert result.status == "failed"
    assert calls == []
    assert store.last_completed_date() is None
    bundle = json.loads(result.bundle_path.read_text(encoding="utf-8"))
    assert bundle["coverage"]["local_source_available"] is False
    assert any(
        finding["code"] == "local_completeness_records_all_invalid"
        for finding in bundle["findings"]
    )


def test_unreadable_empty_local_source_persists_infrastructure_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if an OSError escaped before an unavailable bundle could be persisted."""

    local_path = tmp_path / "graph_trace.jsonl"
    local_path.write_text("placeholder\n", encoding="utf-8")
    original_open = Path.open

    def fail_local_open(self: Path, *args: object, **kwargs: object):
        if self == local_path:
            raise OSError("https://reader:secret@local.invalid denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_local_open)
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    result = _run(
        tmp_path,
        source=FakeSource([]),
        local_trace_path=local_path,
        store=store,
        codex_runner=lambda **_: pytest.fail("empty unavailable evidence must not call Codex"),
    )

    assert result.status == "failed"
    assert store.last_completed_date() is None
    bundle = json.loads(result.bundle_path.read_text(encoding="utf-8"))
    assert any(
        finding["code"] == "local_completeness_read_failed"
        and "reader:secret" not in finding["summary"]
        for finding in bundle["findings"]
    )


def test_first_observation_can_be_code_addressed_with_real_new_repo_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if a first-seen bad trace could not acknowledge a later verified commit."""

    repo, commit_sha = _committed_test_repo(tmp_path, monkeypatch)
    issue = DailyAuditIssue(
        issue_key="tool.first-addressed",
        status="code_addressed",
        title="首次发现时已有代码处理",
        first_seen=AUDIT_DATE,
        last_seen=AUDIT_DATE,
        trace_evidence_refs=["trace:trace-current"],
        code_evidence_refs=[
            f"code:{commit_sha}",
            "test:tests/tdd/example/test_regression.py",
        ],
    )
    result = _run(
        tmp_path,
        source=FakeSource([_trace()]),
        repo_root=repo,
        codex_runner=lambda **_: _report(issue=issue),
    )

    assert result.status == "succeeded"


@pytest.mark.parametrize("bad_commit", ["not-a-commit", "f" * 40])
def test_code_addressed_rejects_forged_or_missing_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_commit: str
) -> None:
    """Would fail if a well-shaped but nonexistent code ref could advance lifecycle state."""

    repo, _ = _committed_test_repo(tmp_path, monkeypatch)
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    prior = DailyAuditIssue(
        issue_key="tool.addressed",
        status="open",
        title="待处理问题",
        first_seen=date(2026, 8, 4),
        last_seen=date(2026, 8, 4),
        trace_evidence_refs=["trace:prior"],
    )
    store.write_issue_registry(IssueRegistry(issues={prior.issue_key: prior}))
    candidate = prior.model_copy(
        update={
            "status": "code_addressed",
            "trace_evidence_refs": ["trace:trace-current"],
            "code_evidence_refs": [
                f"code:{bad_commit}",
                "test:tests/tdd/example/test_regression.py",
            ],
        }
    )
    result = _run(
        tmp_path,
        source=FakeSource([_trace()]),
        store=store,
        repo_root=repo,
        codex_runner=lambda **_: _report(issue=candidate),
    )

    assert result.status == "failed"
    assert store.read_issue_registry().issues[prior.issue_key].status == "open"


def test_code_addressed_rejects_missing_repository_test_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if a nonexistent test ref were accepted as code-addressing evidence."""

    repo, commit_sha = _committed_test_repo(tmp_path, monkeypatch)
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    prior = DailyAuditIssue(
        issue_key="tool.addressed",
        status="open",
        title="待处理问题",
        first_seen=date(2026, 8, 4),
        last_seen=date(2026, 8, 4),
        trace_evidence_refs=["trace:prior"],
    )
    store.write_issue_registry(IssueRegistry(issues={prior.issue_key: prior}))
    candidate = prior.model_copy(
        update={
            "status": "code_addressed",
            "trace_evidence_refs": ["trace:trace-current"],
            "code_evidence_refs": [
                f"code:{commit_sha}",
                "test:tests/tdd/example/test_missing.py",
            ],
        }
    )
    result = _run(
        tmp_path,
        source=FakeSource([_trace()]),
        store=store,
        repo_root=repo,
        codex_runner=lambda **_: _report(issue=candidate),
    )

    assert result.status == "failed"


def test_code_addressed_rejects_commit_older_than_its_bad_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if an older commit could be presented as handling a later bad trace."""

    repo, commit_sha = _committed_test_repo(
        tmp_path,
        monkeypatch,
        committed_at="2026-08-05T11:00:00+00:00",
    )
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    prior = DailyAuditIssue(
        issue_key="tool.time-order",
        status="open",
        title="时序不可信",
        first_seen=date(2026, 8, 4),
        last_seen=date(2026, 8, 4),
        trace_evidence_refs=["trace:prior"],
    )
    store.write_issue_registry(IssueRegistry(issues={prior.issue_key: prior}))
    issue = prior.model_copy(
        update={
            "status": "code_addressed",
            "trace_evidence_refs": ["trace:trace-current"],
            "code_evidence_refs": [
                f"code:{commit_sha}",
                "test:tests/tdd/example/test_regression.py",
            ],
        }
    )
    result = _run(
        tmp_path,
        source=FakeSource([_trace()]),
        store=store,
        repo_root=repo,
        codex_runner=lambda **_: _report(issue=issue),
    )

    assert result.status == "failed"


def test_regressed_issue_cannot_reuse_old_code_evidence() -> None:
    """Would fail if a regression could be suppressed by replaying its obsolete fix evidence."""

    previous_issue = DailyAuditIssue(
        issue_key="tool.regressed",
        status="regressed",
        title="再次出现",
        first_seen=date(2026, 8, 1),
        last_seen=date(2026, 8, 4),
        trace_evidence_refs=["trace:bad", "trace:bad-again"],
        code_evidence_refs=["code:abc123", "test:tests/tdd/example/test_old.py"],
    )
    candidate = previous_issue.model_copy(update={"status": "code_addressed"})

    with pytest.raises(ValueError, match="new code evidence"):
        merge_issue_registry(
            IssueRegistry(issues={previous_issue.issue_key: previous_issue}),
            [candidate],
            AUDIT_DATE,
        )


@pytest.mark.parametrize("status", ["runtime_verified", "regressed"])
def test_explicit_refresh_rejects_terminal_status_for_unknown_issue(
    tmp_path: Path, status: str
) -> None:
    """Would fail if manual non-persistence also disabled lifecycle validation."""

    evidence = (
        {"runtime_verification_refs": ["trace:trace-current"]}
        if status == "runtime_verified"
        else {"trace_evidence_refs": ["trace:trace-current"]}
    )
    issue = DailyAuditIssue(
        issue_key="tool.unknown",
        status=status,
        title="不存在的问题",
        first_seen=AUDIT_DATE,
        last_seen=AUDIT_DATE,
        **evidence,
    )
    result = _run(
        tmp_path,
        source=FakeSource([_trace()]),
        codex_runner=lambda **_: _report(issue=issue),
        commit_continuous_state=False,
    )

    assert result.status == "failed"


def test_report_view_keeps_active_registry_issue_codex_omitted(
    tmp_path: Path
) -> None:
    """Would fail if Codex omission made an active issue disappear from the human report."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    prior = DailyAuditIssue(
        issue_key="tool.persistent",
        status="open",
        title="持续待处理问题",
        first_seen=date(2026, 8, 4),
        last_seen=date(2026, 8, 4),
        trace_evidence_refs=["trace:prior"],
    )
    store.write_issue_registry(IssueRegistry(issues={prior.issue_key: prior}))
    result = _run(
        tmp_path,
        source=FakeSource([_trace()]),
        store=store,
        codex_runner=lambda **_: _report(),
    )

    assert result.status == "succeeded"
    markdown = result.report_path.read_text(encoding="utf-8")
    assert "持续待处理问题" in markdown
    assert "tool.persistent" in markdown


def test_empty_day_keeps_code_addressed_registry_status_without_codex(
    tmp_path: Path
) -> None:
    """Would fail if a quiet day erased the waiting-for-natural-validation status line."""

    local_path = tmp_path / "graph_trace.jsonl"
    local_path.write_text("", encoding="utf-8")
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    prior = DailyAuditIssue(
        issue_key="tool.waiting",
        status="code_addressed",
        title="等待自然验证",
        first_seen=date(2026, 8, 4),
        last_seen=date(2026, 8, 4),
        trace_evidence_refs=["trace:prior"],
        code_evidence_refs=["code:abc123"],
    )
    store.write_issue_registry(IssueRegistry(issues={prior.issue_key: prior}))
    result = _run(
        tmp_path,
        source=FakeSource([]),
        local_trace_path=local_path,
        store=store,
        codex_runner=lambda **_: pytest.fail("empty day must not call Codex"),
    )

    assert result.status == "succeeded"
    markdown = result.report_path.read_text(encoding="utf-8")
    assert "等待自然验证" in markdown
    assert "tool.waiting" in markdown


def test_uncertain_is_observation_not_maintainer_decision() -> None:
    """Would fail if uncertain evidence appeared in the actionable decision section."""

    issue = DailyAuditIssue(
        issue_key="tool.observe",
        status="uncertain",
        title="继续观察",
        first_seen=AUDIT_DATE,
        last_seen=AUDIT_DATE,
        trace_evidence_refs=["trace:trace-current"],
    )
    markdown = render_daily_codex_report(_report(issue=issue))
    decision, observation = markdown.split("## 需要继续观察", maxsplit=1)

    assert "继续观察" not in decision.split("## 需要你决定", maxsplit=1)[1]
    assert "继续观察" in observation
    assert "tool.observe" in markdown


def test_trace_score_combination_ref_must_match_current_bundle(tmp_path: Path) -> None:
    """Would fail if safe Score evidence could not be authenticated to its current trace."""

    score = LangfuseScoreSnapshot(
        score_id="score-current",
        name="assistant_agent.quality.response_quality",
        value=0.2,
    )
    issue = DailyAuditIssue(
        issue_key="quality.low",
        status="uncertain",
        title="评分偏低",
        first_seen=AUDIT_DATE,
        last_seen=AUDIT_DATE,
        trace_evidence_refs=["trace:trace-current/score:score-current"],
    )
    result = _run(
        tmp_path,
        source=FakeSource([_trace(scores=[score])]),
        codex_runner=lambda **_: _report(issue=issue),
    )

    assert result.status == "succeeded"


def test_url_userinfo_is_removed_before_failed_attempt_is_persisted(tmp_path: Path) -> None:
    """Would fail if attempt JSON retained credentials already hidden by Markdown rendering."""

    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    result = _run(
        tmp_path,
        source=FakeSource([_trace()]),
        store=store,
        codex_runner=lambda **_: (_ for _ in ()).throw(
            RuntimeError("https://alice:secret@audit.invalid failed")
        ),
    )

    assert result.status == "failed"
    attempt = DailyAuditAttempt.model_validate_json(
        (store.attempts_dir / f"{result.attempt_id}.json").read_text(encoding="utf-8")
    )
    assert "alice:secret" not in (attempt.error_summary or "")
    assert "alice:secret" not in result.model_dump_json()


def test_daily_schema_bounds_human_text_and_arrays() -> None:
    """Would fail if structured output allowed unbounded report prose or issue fan-out."""

    with pytest.raises(ValidationError):
        _report(daily_summary="x" * 2_001)
    with pytest.raises(ValidationError):
        DailyCodexAuditReport(
            audit_date=AUDIT_DATE,
            daily_summary="结论",
            activity_summary="概况",
            issues=[
                DailyAuditIssue(
                    issue_key=f"tool.issue-{index}",
                    status="uncertain",
                    title="观察",
                    first_seen=AUDIT_DATE,
                    last_seen=AUDIT_DATE,
                )
                for index in range(51)
            ],
            memory_summary="记忆",
            infrastructure_summary="系统",
        )
    schema = runner_module.daily_codex_report_json_schema()
    assert schema["properties"]["issues"]["maxItems"] == 50
    assert schema["properties"]["daily_summary"]["maxLength"] == 2_000


def test_historical_registry_issue_text_remains_readable_beyond_codex_limits() -> None:
    """Would fail if new output bounds invalidated already persisted issue history."""

    historic = DailyAuditIssue(
        issue_key="tool.historical",
        status="open",
        title="历史标题" * 200,
        plain_summary="历史正文" * 600,
        first_seen=date(2026, 8, 1),
        last_seen=date(2026, 8, 2),
        trace_evidence_refs=["trace:historic"],
    )

    registry = IssueRegistry.model_validate_json(
        IssueRegistry(issues={historic.issue_key: historic}).model_dump_json()
    )

    assert registry.issues[historic.issue_key].plain_summary == historic.plain_summary


def test_issue_key_rejects_markdown_controls_before_appendix_rendering() -> None:
    """Would fail if a stable appendix key could escape its Markdown code span."""

    with pytest.raises(ValidationError, match="issue_key"):
        DailyCodexAuditReport(
            audit_date=AUDIT_DATE,
            daily_summary="结论",
            activity_summary="概况",
            issues=[
                {
                    "issue_key": "tool.`injected`",
                    "status": "open",
                    "title": "问题",
                    "first_seen": AUDIT_DATE,
                    "last_seen": AUDIT_DATE,
                }
            ],
            memory_summary="记忆",
            infrastructure_summary="系统",
        )


def test_historical_unsafe_issue_key_is_readable_but_safely_rendered() -> None:
    """Would fail if appendix hardening made a legacy registry unreadable or injectable."""

    issue = DailyAuditIssue(
        issue_key="tool.`historic`",
        status="open",
        title="历史问题",
        first_seen=date(2026, 8, 1),
        last_seen=date(2026, 8, 2),
        trace_evidence_refs=["trace:historic"],
    )
    registry = IssueRegistry.model_validate_json(
        json.dumps(
            {
                "schema_version": "assistant_agent_runtime_audit_issues_v1",
                "issues": {issue.issue_key: issue.model_dump(mode="json")},
            }
        )
    )
    markdown = render_daily_codex_report(_report(), issues=[registry.issues[issue.issue_key]])

    assert "tool.`historic`" not in markdown
    assert "tool._historic_" in markdown


def test_codex_prompt_forbids_copying_sensitive_source_bodies(tmp_path: Path) -> None:
    """Would fail if the isolated reviewer was not told to minimize sensitive content."""

    captured: dict[str, object] = {}
    output_path = tmp_path / "output.json"

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["input"] = kwargs["input"]
        output_path.write_text(_report().model_dump_json(), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    runner_module.run_daily_codex_report(
        audit_date=AUDIT_DATE,
        bundle_path=tmp_path / "bundle.json",
        issues_path=tmp_path / "issues.json",
        repo_root=tmp_path,
        output_path=output_path,
        schema_path=tmp_path / "schema.json",
        process_runner=fake_run,
    )

    prompt = str(captured["input"])
    assert "不得复制完整用户对话" in prompt
    assert "不得复制 Memory 正文" in prompt
    assert "不得复制 Provider 原始响应" in prompt
    assert "code:<commit-sha>" in prompt
    assert "test:<repo-relative-path>" in prompt


def test_sensitive_long_text_overlap_keeps_internal_json_but_blocks_success(
    tmp_path: Path
) -> None:
    """Would fail if copied user content could be published and advance continuous state."""

    sensitive = "用户隐私正文" * 30
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")

    def codex_runner(*, output_path: Path, **_: object) -> DailyCodexAuditReport:
        report = _report(daily_summary=sensitive)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report.model_dump_json(), encoding="utf-8")
        return report

    result = _run(
        tmp_path,
        source=FakeSource([_trace(input={"messages": [{"content": sensitive}]})]),
        store=store,
        codex_runner=codex_runner,
    )

    assert result.status == "failed"
    assert store.codex_json_path(result.attempt_id).exists()
    assert store.last_completed_date() is None
    assert "审计未完成" in store.daily_report_path(AUDIT_DATE).read_text(encoding="utf-8")
    assert sensitive not in store.daily_report_path(AUDIT_DATE).read_text(encoding="utf-8")


def test_dry_run_corrupted_watermark_returns_stable_json_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would fail if dry-run escaped the CLI exception boundary with a traceback."""

    watermark = tmp_path / ".data/runtime_audit/state/watermark.json"
    watermark.parent.mkdir(parents=True)
    watermark.write_text("{broken", encoding="utf-8")

    exit_code = main(
        ["--no-env-file", "--repo-root", str(tmp_path), "run", "--dry-run"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    payload = json.loads(captured.err)
    assert payload["status"] == "failed"
    assert payload["error_type"] == "JSONDecodeError"
    assert "Traceback" not in captured.err

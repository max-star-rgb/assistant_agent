from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from assistant_agent.observability.runtime_audit.collector import collect_runtime_audit
from assistant_agent.observability.runtime_audit.models import LangfuseTraceSnapshot
from assistant_agent.observability.runtime_audit.models import CodexAuditReport
from assistant_agent.observability.runtime_audit.langfuse_source import LangfuseSdkAuditSource
from assistant_agent.observability.runtime_audit.online_evaluators import (
    configure_native_online_evaluators,
)
from assistant_agent.observability.runtime_audit.runner import (
    build_codex_command,
    codex_report_json_schema,
    run_codex_report,
    sanitized_codex_environment,
)
from assistant_agent.observability.runtime_audit import daily_models as daily_models_module
from assistant_agent.observability.runtime_audit import runner as runner_module
from assistant_agent.observability.runtime_audit.storage import RuntimeAuditArtifactStore
from assistant_agent.observability.runtime_audit.report import render_deterministic_report
from evals.agent.contracts import AssertionResult, DimensionResult
from evals.agent.langfuse_backend import _evaluations


UTC = timezone.utc


class FakeLangfuseSource:
    def __init__(self, traces: list[LangfuseTraceSnapshot]) -> None:
        self.traces = traces

    def list_traces(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[LangfuseTraceSnapshot]:
        return [
            trace
            for trace in self.traces
            if window_start <= trace.timestamp <= window_end
        ]


def _write_event(
    path: Path,
    *,
    trace_id: str,
    run_id: str,
    canonical_event: str,
    created_at: datetime,
) -> None:
    payload = {
        "trace_id": trace_id,
        "run_id": run_id,
        "node_name": "runtime",
        "event_type": "observability",
        "canonical_event": canonical_event,
        "status": "completed" if canonical_event == "run.completed" else None,
        "created_at": created_at.isoformat(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def test_local_timeline_is_only_attached_when_langfuse_export_is_missing(
    tmp_path: Path,
) -> None:
    """Would fail if local events became the normal audit source again."""

    now = datetime(2026, 8, 5, 4, 0, tzinfo=UTC)
    trace_path = tmp_path / "graph_trace.jsonl"
    for trace_id in ("trace-exported", "trace-missing"):
        _write_event(
            trace_path,
            trace_id=trace_id,
            run_id=f"run-{trace_id}",
            canonical_event="run.started",
            created_at=now - timedelta(minutes=30),
        )
        _write_event(
            trace_path,
            trace_id=trace_id,
            run_id=f"run-{trace_id}",
            canonical_event="run.completed",
            created_at=now - timedelta(minutes=29),
        )

    source = FakeLangfuseSource(
        [
            LangfuseTraceSnapshot(
                trace_id="trace-exported",
                name="assistant.turn",
                timestamp=now - timedelta(minutes=30),
                observations=[],
                scores=[],
            )
        ]
    )

    bundle = collect_runtime_audit(
        source=source,
        local_trace_path=trace_path,
        window_start=now - timedelta(hours=2),
        window_end=now,
        collected_at=now,
    )

    assert bundle.coverage.langfuse_trace_count == 1
    assert bundle.coverage.local_trace_count == 2
    assert [item.trace_id for item in bundle.local_fallbacks] == ["trace-missing"]
    assert bundle.local_fallbacks[0].event_count == 2
    assert {event.canonical_event for event in bundle.local_fallbacks[0].timeline} == {
        "run.started",
        "run.completed",
    }
    assert any(
        finding.code == "langfuse_export_missing"
        and finding.trace_id == "trace-missing"
        and finding.category == "coverage"
        for finding in bundle.findings
    )


def test_judge_grace_is_pending_not_a_quality_failure() -> None:
    """Would fail if asynchronous evaluator latency were counted as poor quality."""

    now = datetime(2026, 8, 5, 4, 0, tzinfo=UTC)
    source = FakeLangfuseSource(
        [
            LangfuseTraceSnapshot(
                trace_id="trace-young",
                name="assistant.turn",
                timestamp=now - timedelta(minutes=3),
                observations=[
                    {
                        "observation_id": "response-1",
                        "name": "assistant.response",
                        "type": "SPAN",
                    }
                ],
                scores=[],
            )
        ]
    )

    bundle = collect_runtime_audit(
        source=source,
        local_trace_path=None,
        window_start=now - timedelta(hours=2),
        window_end=now,
        collected_at=now,
        judge_grace=timedelta(minutes=15),
    )

    pending = [item for item in bundle.findings if item.code == "judge_pending"]
    assert {item.score_name for item in pending} == {
        "assistant_agent.quality.response_quality",
        "assistant_agent.quality.grounding",
    }
    assert all(item.quality_failure is False for item in pending)


def test_old_trace_reports_missing_duplicate_and_low_quality_scores() -> None:
    """Would fail if Score name/scope reconciliation silently accepted bad coverage."""

    now = datetime(2026, 8, 5, 4, 0, tzinfo=UTC)
    trace = LangfuseTraceSnapshot(
        trace_id="trace-old",
        name="assistant.turn",
        timestamp=now - timedelta(hours=1),
        observations=[
            {
                "observation_id": "response-1",
                "name": "assistant.response",
                "type": "SPAN",
            },
            {
                "observation_id": "tool-1",
                "name": "tool.calendar_search",
                "type": "SPAN",
            },
            {
                "observation_id": "memory-1",
                "name": "memory.turn_ingestion",
                "type": "SPAN",
                "input": {
                    "messages": [
                        {"role": "user", "content": "prefers Chinese"},
                        {"role": "assistant", "content": "acknowledged"},
                    ]
                },
                "output": {"changes": [{"event": "ADD", "memory": "prefers Chinese"}]},
            },
            {
                "observation_id": "memory-queued",
                "name": "memory.turn_ingestion",
                "type": "EVENT",
            },
            {
                "observation_id": "memory-no-content",
                "name": "memory.turn_ingestion",
                "type": "SPAN",
                "output": {"memory_count": 1, "content_exported": False},
            },
        ],
        scores=[
            {
                "score_id": "score-response",
                "name": "assistant_agent.quality.response_quality",
                "value": 0.2,
                "observation_id": "response-1",
            },
            {
                "score_id": "score-grounding-a",
                "name": "assistant_agent.quality.grounding",
                "value": 1.0,
                "observation_id": "response-1",
            },
            {
                "score_id": "score-grounding-b",
                "name": "assistant_agent.quality.grounding",
                "value": 1.0,
                "observation_id": "response-1",
            },
        ],
    )

    bundle = collect_runtime_audit(
        source=FakeLangfuseSource([trace]),
        local_trace_path=None,
        window_start=now - timedelta(hours=2),
        window_end=now,
        collected_at=now,
    )

    codes = {(item.code, item.score_name, item.observation_id) for item in bundle.findings}
    assert (
        "score_low",
        "assistant_agent.quality.response_quality",
        "response-1",
    ) in codes
    assert (
        "score_duplicate",
        "assistant_agent.quality.grounding",
        "response-1",
    ) in codes
    assert (
        "score_missing",
        "assistant_agent.quality.tool_result_quality",
        "tool-1",
    ) in codes
    assert (
        "score_missing",
        "assistant_agent.quality.memory_extraction",
        "memory-1",
    ) in codes
    assert not any(item.observation_id == "memory-queued" for item in bundle.findings)
    assert any(
        item.code == "memory_extraction_evidence_missing"
        and item.observation_id == "memory-no-content"
        and item.quality_failure is False
        for item in bundle.findings
    )
    assert not any(
        item.code == "score_missing" and item.observation_id == "memory-no-content"
        for item in bundle.findings
    )
    assert all(
        item.quality_failure
        for item in bundle.findings
        if item.code == "score_low"
    )
    assert all(
        item.quality_failure is False and item.category == "infrastructure"
        for item in bundle.findings
        if item.code in {"score_missing", "score_duplicate"}
    )


def test_specific_tool_names_still_require_tool_result_quality_scores() -> None:
    """Would fail if audit coverage guessed tool execution from observation names."""

    now = datetime(2026, 8, 5, 4, 0, tzinfo=UTC)
    trace = LangfuseTraceSnapshot(
        trace_id="trace-specific-tool-names",
        name="assistant.turn",
        timestamp=now - timedelta(hours=1),
        observations=[
            {
                "observation_id": "response-1",
                "name": "assistant.response",
                "type": "SPAN",
            },
            {
                "observation_id": "tool-marker",
                "name": "shopping_search",
                "type": "SPAN",
                "metadata": {
                    "assistant_agent.observation_kind": "tool_execution",
                },
            },
            {
                "observation_id": "tool-legacy",
                "name": "image_generation",
                "type": "SPAN",
                "metadata": {
                    "attributes": {
                        "assistant_agent.canonical_event": "tool.finished",
                    }
                },
            },
        ],
        scores=[],
    )

    bundle = collect_runtime_audit(
        source=FakeLangfuseSource([trace]),
        local_trace_path=None,
        window_start=now - timedelta(hours=2),
        window_end=now,
        collected_at=now,
    )

    missing_tool_scores = {
        item.observation_id
        for item in bundle.findings
        if item.code == "score_missing"
        and item.score_name == "assistant_agent.quality.tool_result_quality"
    }
    assert missing_tool_scores == {"tool-marker", "tool-legacy"}


def test_codex_runner_uses_read_only_ephemeral_mode_and_removes_credentials(
    tmp_path: Path,
) -> None:
    """Would fail if the report subprocess inherited service credentials or write access."""

    output_path = tmp_path / "report.json"
    schema_path = tmp_path / "schema.json"
    command = build_codex_command(
        repo_root=tmp_path,
        output_path=output_path,
        schema_path=schema_path,
    )
    environment = sanitized_codex_environment(
        {
            "PATH": "/usr/bin",
            "LANGFUSE_PUBLIC_KEY": "pk-secret",
            "LANGFUSE_SECRET_KEY": "sk-secret",
            "OPENAI_API_KEY": "openai-secret",
            "DASHSCOPE_API_KEY": "dashscope-secret",
            "ASSISTANT_AGENT_LANGFUSE_SECRET_KEY": "assistant-secret",
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
        }
    )

    assert command == [
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--cd",
        str(tmp_path),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-",
    ]
    assert environment == {
        "PATH": "/usr/bin",
        "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
    }


def test_langfuse_source_paginates_headers_then_fetches_full_trace_details() -> None:
    """Would fail if the collector scanned only the first page or omitted observations/Scores."""

    now = datetime(2026, 8, 5, 4, 0, tzinfo=UTC)

    class TraceApi:
        def __init__(self) -> None:
            self.pages: list[int] = []

        def list(self, **kwargs):
            page = kwargs["page"]
            self.pages.append(page)
            trace_id = f"trace-{page}"
            return SimpleNamespace(
                data=[SimpleNamespace(id=trace_id)],
                meta=SimpleNamespace(total_pages=2),
            )

        def get(self, trace_id: str):
            return {
                "id": trace_id,
                "name": "assistant.turn",
                "timestamp": now - timedelta(minutes=10),
                "observations": [
                    {
                        "id": f"observation-{trace_id}",
                        "name": "agent.runtime",
                        "type": "SPAN",
                    }
                ],
                "scores": [],
            }

    trace_api = TraceApi()

    class ScoresApi:
        def get_many_v3(self, **kwargs):
            trace_id = kwargs["trace_id"]
            return SimpleNamespace(
                data=[
                    {
                        "id": f"score-{trace_id}",
                        "name": "assistant_agent.quality.response_quality",
                        "value": 1.0,
                        "subject": {
                            "kind": "observation",
                            "id": f"observation-{trace_id}",
                            "trace_id": trace_id,
                        },
                    }
                ],
                meta=SimpleNamespace(cursor=None),
            )

    client = SimpleNamespace(
        api=SimpleNamespace(trace=trace_api, scores_v3=ScoresApi())
    )
    source = LangfuseSdkAuditSource(client, page_size=50)

    traces = source.list_traces(
        window_start=now - timedelta(hours=2),
        window_end=now,
    )

    assert trace_api.pages == [1, 2]
    assert [trace.trace_id for trace in traces] == ["trace-1", "trace-2"]
    assert traces[0].observations[0].observation_id == "observation-trace-1"
    assert traces[0].scores[0].score_id == "score-trace-1"


def test_artifact_store_writes_versioned_bundle_latest_pointer_and_read_only_report(
    tmp_path: Path,
) -> None:
    """Would fail if the hourly run could not resume or its report implied mutation."""

    now = datetime(2026, 8, 5, 4, 0, tzinfo=UTC)
    bundle = collect_runtime_audit(
        source=FakeLangfuseSource([]),
        local_trace_path=None,
        window_start=now - timedelta(hours=2),
        window_end=now,
        collected_at=now,
    )
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")

    bundle_path = store.write_bundle(bundle)
    markdown_path = store.write_deterministic_report(
        bundle,
        render_deterministic_report(bundle),
    )

    persisted = json.loads(bundle_path.read_text(encoding="utf-8"))
    latest_bundle = json.loads(store.latest_bundle_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert persisted["schema_version"] == "assistant_agent_runtime_audit_bundle_v2"
    assert persisted["production_mutation_allowed"] is False
    assert latest_bundle == {
        "schema_version": "assistant_agent_runtime_audit_watermark_v1",
        "audit_run_id": bundle.audit_run_id,
        "last_window_end": now.isoformat().replace("+00:00", "Z"),
        "bundle_path": str(bundle_path),
    }
    assert not store.watermark_path.exists()
    assert "No production, code, Langfuse, or memory mutation was performed." in markdown


def test_codex_report_runner_validates_structured_output_without_passing_bundle_in_prompt(
    tmp_path: Path,
) -> None:
    """Would fail if the report boundary accepted arbitrary prose or leaked the bundle via argv."""

    bundle_path = tmp_path / "inbox" / "audit.json"
    bundle_path.parent.mkdir()
    bundle_path.write_text('{"audit_run_id":"runtime_audit_test"}', encoding="utf-8")
    output_path = tmp_path / "reports" / "audit.json"
    schema_path = tmp_path / "state" / "schema.json"
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        captured["env"] = kwargs["env"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            CodexAuditReport(
                audit_run_id="runtime_audit_test",
                executive_summary="No critical issue.",
                coverage_assessment="Complete for the selected window.",
            ).model_dump_json(),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    report = run_codex_report(
        bundle_path=bundle_path,
        repo_root=tmp_path,
        output_path=output_path,
        schema_path=schema_path,
        environment={"PATH": "/usr/bin", "LANGFUSE_SECRET_KEY": "do-not-leak"},
        process_runner=fake_run,
    )

    assert report.audit_run_id == "runtime_audit_test"
    assert schema_path.exists()
    assert "do-not-leak" not in str(captured)
    assert str(bundle_path) in str(captured["input"])
    assert str(bundle_path) not in captured["command"]
    assert captured["env"] == {
        "PATH": "/usr/bin",
        "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
    }


def test_codex_report_runner_uses_explicit_executable_outside_service_path(
    tmp_path: Path,
) -> None:
    """Would fail if a systemd service had to discover Codex through its reduced PATH."""

    bundle_path = tmp_path / "inbox" / "audit.json"
    bundle_path.parent.mkdir()
    bundle_path.write_text('{"audit_run_id":"runtime_audit_test"}', encoding="utf-8")
    output_path = tmp_path / "reports" / "audit.json"
    schema_path = tmp_path / "state" / "schema.json"
    captured: dict[str, object] = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            CodexAuditReport(
                audit_run_id="runtime_audit_test",
                executive_summary="No critical issue.",
                coverage_assessment="Complete for the selected window.",
            ).model_dump_json(),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    run_codex_report(
        bundle_path=bundle_path,
        repo_root=tmp_path,
        output_path=output_path,
        schema_path=schema_path,
        environment={
            "PATH": "/usr/bin",
            "ASSISTANT_AGENT_CODEX_EXECUTABLE": "/opt/codex/bin/codex",
        },
        process_runner=fake_run,
    )

    assert captured["command"][0] == "/opt/codex/bin/codex"


def test_daily_codex_runner_uses_issue_state_and_daily_schema_in_stdin(
    tmp_path: Path,
) -> None:
    """Would fail if daily Codex received state in argv or could return a rolling report."""

    audit_date = datetime(2026, 8, 5, tzinfo=UTC).date()
    bundle_path = tmp_path / "inbox" / "daily.json"
    issues_path = tmp_path / "state" / "issues.json"
    output_path = tmp_path / "state" / "attempts" / "daily.codex.json"
    schema_path = tmp_path / "state" / "schemas" / "daily.schema.json"
    bundle_path.parent.mkdir()
    issues_path.parent.mkdir()
    bundle_path.write_text('{"schema_version":"assistant_agent_runtime_audit_bundle_v1"}', encoding="utf-8")
    issues_path.write_text('{"schema_version":"assistant_agent_runtime_audit_issues_v1","issues":{}}', encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            daily_models_module.DailyCodexAuditReport(
                audit_date=audit_date,
                daily_summary="没有需要处理的问题。",
                activity_summary="昨日无可审计对话。",
                memory_summary="没有记忆问题。",
                infrastructure_summary="审计任务运行正常。",
            ).model_dump_json(),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    report = runner_module.run_daily_codex_report(
        audit_date=audit_date,
        bundle_path=bundle_path,
        issues_path=issues_path,
        repo_root=tmp_path,
        output_path=output_path,
        schema_path=schema_path,
        environment={"PATH": "/usr/bin", "LANGFUSE_SECRET_KEY": "do-not-leak"},
        process_runner=fake_run,
    )

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert report.audit_date == audit_date
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert str(bundle_path) in str(captured["input"])
    assert str(issues_path) in str(captured["input"])
    assert str(bundle_path) not in captured["command"]
    assert str(issues_path) not in captured["command"]
    assert "报告读者是项目维护者，不是另一个 Codex。" in str(captured["input"])
    assert "production_mutation_allowed 必须为 false" in str(captured["input"])
    assert "除输入已有机器证据外，不得声称已运行测试、已部署、已在生产或真实 trace 验证。" in str(captured["input"])
    assert "不得把推测写成事实。" in str(captured["input"])


def test_codex_environment_removes_credentials_and_proxies_but_preserves_codex_login_home() -> None:
    """Would fail if report subprocess leaked credentials or lost Codex's controlled login home."""

    sanitized = runner_module.sanitized_codex_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/controlled/codex-home",
            "CODEX_HOME": "/controlled/codex-home/.codex",
            "LANGFUSE_SECRET_KEY": "redacted",
            "APP_TOKEN": "redacted",
            "HTTPS_PROXY": "http://credential@proxy.invalid",
            "HTTP_PROXY": "http://credential@proxy.invalid",
            "ALL_PROXY": "socks5://credential@proxy.invalid",
            "SERVICE_HTTPS_PROXY_URL": "http://credential@proxy.invalid",
            "DATABASE_URL": "redacted",
            "GITHUB_PAT": "redacted",
            "PRIVATE_KEY": "redacted",
            "UNRELATED_RUNTIME_SETTING": "must-not-pass-through",
        }
    )

    assert sanitized == {
        "PATH": "/usr/bin",
        "HOME": "/controlled/codex-home",
        "CODEX_HOME": "/controlled/codex-home/.codex",
        "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
    }


def test_codex_environment_has_an_explicit_minimal_startup_allowlist() -> None:
    """Would fail if an unlisted runtime variable reached the isolated Codex process."""

    sanitized = runner_module.sanitized_codex_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/controlled/codex-home",
            "CODEX_HOME": "/controlled/codex-home/.codex",
            "ASSISTANT_AGENT_CODEX_EXECUTABLE": "/opt/codex/bin/codex",
            "LANG": "zh_CN.UTF-8",
            "LC_ALL": "zh_CN.UTF-8",
            "LC_CTYPE": "zh_CN.UTF-8",
            "LC_API_KEY": "redacted",
            "LC_DATABASE_URL": "redacted",
            "TERM": "xterm-256color",
            "TMPDIR": "/tmp/codex",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
            "SSL_CERT_DIR": "/etc/ssl/certs",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/custom.pem",
            "DATABASE_URL": "redacted",
            "GITHUB_PAT": "redacted",
            "PRIVATE_KEY": "redacted",
            "OTHER_PROXY_VALUE": "http://credential@proxy.invalid",
            "UNLISTED_VALUE": "must-not-pass-through",
        }
    )

    assert sanitized == {
        "PATH": "/usr/bin",
        "HOME": "/controlled/codex-home",
        "CODEX_HOME": "/controlled/codex-home/.codex",
        "ASSISTANT_AGENT_CODEX_EXECUTABLE": "/opt/codex/bin/codex",
        "LANG": "zh_CN.UTF-8",
        "LC_ALL": "zh_CN.UTF-8",
        "LC_CTYPE": "zh_CN.UTF-8",
        "TERM": "xterm-256color",
        "TMPDIR": "/tmp/codex",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "SSL_CERT_DIR": "/etc/ssl/certs",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/custom.pem",
        "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
    }


def test_langfuse_read_failure_is_infrastructure_unknown_not_missing_export(
    tmp_path: Path,
) -> None:
    """Would fail if a Langfuse outage were misclassified as trace or quality failures."""

    now = datetime(2026, 8, 5, 4, 0, tzinfo=UTC)
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_event(
        trace_path,
        trace_id="trace-local",
        run_id="run-local",
        canonical_event="run.completed",
        created_at=now - timedelta(minutes=20),
    )

    class FailedSource:
        def list_traces(self, **_):
            raise RuntimeError("Authorization: Bearer private-token")

    bundle = collect_runtime_audit(
        source=FailedSource(),
        local_trace_path=trace_path,
        window_start=now - timedelta(hours=2),
        window_end=now,
        collected_at=now,
    )

    assert bundle.coverage.langfuse_source_available is False
    assert bundle.coverage.missing_export_count == 0
    assert bundle.local_fallbacks == []
    assert [(item.code, item.category, item.quality_failure) for item in bundle.findings] == [
        ("langfuse_read_failed", "infrastructure", False)
    ]
    assert "private-token" not in bundle.findings[0].summary


def test_experiment_publishes_canonical_scores_and_keeps_tool_quality_observation_scoped() -> None:
    """Would fail if legacy agent_eval names or task-level tool semantics returned."""

    dimension = DimensionResult(
        passed=True,
        reason="passed",
        assertions={
            "criterion": AssertionResult(
                passed=True,
                label="criterion",
                reason="passed",
                evaluation_method="rule",
            )
        },
    )
    result = SimpleNamespace(
        dimensions=SimpleNamespace(
            tool_execution=dimension,
            tool_semantics=dimension,
            grounding=dimension,
            response_quality=dimension,
        )
    )

    evaluations = _evaluations(result)

    assert [item.name for item in evaluations] == [
        "assistant_agent.quality.task_conformance",
        "assistant_agent.quality.grounding",
        "assistant_agent.quality.response_quality",
    ]
    assert all(item.data_type == "BOOLEAN" for item in evaluations)


def test_codex_report_schema_is_strict_for_every_object() -> None:
    """Would fail with Codex invalid_json_schema before a report request starts."""

    schema = codex_report_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    recommendation = schema["$defs"]["AuditRecommendation"]
    assert recommendation["additionalProperties"] is False
    assert set(recommendation["required"]) == set(recommendation["properties"])


def test_orphan_local_side_stream_is_not_counted_as_a_missing_turn_export(
    tmp_path: Path,
) -> None:
    """Would fail if a standalone recall event inflated missing assistant.turn coverage."""

    now = datetime(2026, 8, 5, 4, 0, tzinfo=UTC)
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_event(
        trace_path,
        trace_id="recall-side-stream",
        run_id="session-start",
        canonical_event="memory.session_recall.finished",
        created_at=now - timedelta(minutes=30),
    )

    bundle = collect_runtime_audit(
        source=FakeLangfuseSource([]),
        local_trace_path=trace_path,
        window_start=now - timedelta(hours=2),
        window_end=now,
        collected_at=now,
    )

    assert bundle.coverage.local_trace_count == 0
    assert bundle.coverage.missing_export_count == 0
    assert bundle.local_manifests == []
    assert bundle.local_fallbacks[0].trace_id == "recall-side-stream"
    assert [(item.code, item.category) for item in bundle.findings] == [
        ("local_side_stream_unmatched", "coverage")
    ]


def test_native_online_evaluator_configuration_uses_canonical_names_and_full_sampling() -> None:
    """Would fail if daily traces stayed outside Langfuse native evaluation rules."""

    class Resource:
        def __init__(self) -> None:
            self.created = []

        def list(self):
            return SimpleNamespace(data=[])

        def create(self, *, request):
            self.created.append(request)
            return SimpleNamespace(name=request.name, scope="project")

    evaluators = Resource()
    rules = Resource()
    client = SimpleNamespace(
        api=SimpleNamespace(
            unstable=SimpleNamespace(
                evaluators=evaluators,
                evaluation_rules=rules,
            )
        )
    )

    result = configure_native_online_evaluators(
        client,
        apply=True,
        model_provider="qwen",
        model="qwen3.6-flash",
    )

    assert [item.name for item in evaluators.created] == [
        "assistant_agent.quality.response_quality",
        "assistant_agent.quality.grounding",
        "assistant_agent.quality.tool_result_quality",
        "assistant_agent.quality.memory_extraction",
        "assistant_agent.quality.memory_recall",
    ]
    assert len(rules.created) == 5
    assert [item.name for item in rules.created] == [
        "assistant_agent.quality.response_quality",
        "assistant_agent.quality.grounding",
        "assistant_agent.quality.tool_result_quality",
        "assistant_agent.quality.memory_extraction",
        "assistant_agent.quality.memory_recall",
    ]
    assert all(item.enabled is True and item.sampling == 1.0 for item in rules.created)
    tool_rule = next(
        item
        for item in rules.created
        if item.name == "assistant_agent.quality.tool_result_quality"
    )
    tool_filters = [item.model_dump(mode="json") for item in tool_rule.filter]
    assert not any(item["column"] == "name" for item in tool_filters)
    assert {
        "column": "metadata",
        "key": "assistant_agent.observation_kind",
        "operator": "=",
        "type": "stringObject",
        "value": "tool_execution",
    } in tool_filters
    assert result.applied is True
    assert result.created_evaluators == 5
    assert result.created_rules == 5


def test_native_online_evaluator_configuration_renames_legacy_rules_in_place() -> None:
    """Would fail if migration created duplicate judges or kept legacy Score names."""

    canonical_names = [
        "assistant_agent.quality.response_quality",
        "assistant_agent.quality.grounding",
        "assistant_agent.quality.tool_result_quality",
        "assistant_agent.quality.memory_extraction",
        "assistant_agent.quality.memory_recall",
    ]
    legacy_names = [
        "assistant-agent-live-response-quality",
        "assistant-agent-live-grounding",
        "assistant-agent-live-tool-result-quality",
        "assistant-agent-live-memory-extraction",
        "assistant-agent-live-memory-recall",
    ]

    class EvaluatorResource:
        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(name=name, scope="project")
                    for name in canonical_names
                ]
            )

        def create(self, *, request):
            raise AssertionError(f"unexpected evaluator create: {request.name}")

    class RuleResource:
        def __init__(self) -> None:
            self.created = []
            self.updated = []

        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(id=f"legacy-rule-{index}", name=name)
                    for index, name in enumerate(legacy_names)
                ]
            )

        def create(self, *, request):
            self.created.append(request)

        def update(self, rule_id, **changes):
            self.updated.append((rule_id, changes))

    rules = RuleResource()
    client = SimpleNamespace(
        api=SimpleNamespace(
            unstable=SimpleNamespace(
                evaluators=EvaluatorResource(),
                evaluation_rules=rules,
            )
        )
    )

    result = configure_native_online_evaluators(
        client,
        apply=True,
        model_provider="qwen",
        model="qwen3.6-flash",
    )

    assert rules.created == []
    assert len(rules.updated) == 5
    assert [item[0] for item in rules.updated] == [
        f"legacy-rule-{index}" for index in range(5)
    ]
    assert [item[1]["name"] for item in rules.updated] == canonical_names
    migrated_tool_filters = [
        item.model_dump(mode="json")
        for item in rules.updated[2][1]["filter"]
    ]
    assert not any(item["column"] == "name" for item in migrated_tool_filters)
    assert {
        "column": "metadata",
        "key": "assistant_agent.observation_kind",
        "operator": "=",
        "type": "stringObject",
        "value": "tool_execution",
    } in migrated_tool_filters
    assert result.rule_names == canonical_names
    assert result.existing_evaluators == 5
    assert result.existing_rules == 5
    assert result.updated_rules == 5


def test_native_online_evaluator_configuration_reconciles_existing_tool_rule() -> None:
    """Would fail if an existing name-based tool rule never received the metadata filter."""

    canonical_names = [
        "assistant_agent.quality.response_quality",
        "assistant_agent.quality.grounding",
        "assistant_agent.quality.tool_result_quality",
        "assistant_agent.quality.memory_extraction",
        "assistant_agent.quality.memory_recall",
    ]

    class EvaluatorResource:
        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(name=name, scope="project")
                    for name in canonical_names
                ]
            )

        def create(self, *, request):
            raise AssertionError(f"unexpected evaluator create: {request.name}")

    class RuleResource:
        def __init__(self) -> None:
            self.updated = []

        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(id=f"rule-{index}", name=name)
                    for index, name in enumerate(canonical_names)
                ]
            )

        def create(self, *, request):
            raise AssertionError(f"unexpected rule create: {request.name}")

        def update(self, rule_id, **changes):
            self.updated.append((rule_id, changes))

    rules = RuleResource()
    client = SimpleNamespace(
        api=SimpleNamespace(
            unstable=SimpleNamespace(
                evaluators=EvaluatorResource(),
                evaluation_rules=rules,
            )
        )
    )

    result = configure_native_online_evaluators(
        client,
        apply=True,
        model_provider="qwen",
        model="qwen3.6-flash",
    )

    assert len(rules.updated) == 1
    rule_id, changes = rules.updated[0]
    assert rule_id == "rule-2"
    filters = [item.model_dump(mode="json") for item in changes["filter"]]
    assert not any(item["column"] == "name" for item in filters)
    assert {
        "column": "metadata",
        "key": "assistant_agent.observation_kind",
        "operator": "=",
        "type": "stringObject",
        "value": "tool_execution",
    } in filters
    assert result.existing_rules == 5
    assert result.updated_rules == 1

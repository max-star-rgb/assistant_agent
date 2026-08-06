"""Isolated Codex report runner helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

from assistant_agent.observability.runtime_audit.daily_models import DailyCodexAuditReport
from assistant_agent.observability.runtime_audit.models import CodexAuditReport
from assistant_agent.providers.provider_errors import sanitize_error_message


_CODEX_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "CODEX_HOME",
        "ASSISTANT_AGENT_CODEX_EXECUTABLE",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
)


def build_codex_command(
    *,
    repo_root: Path,
    output_path: Path,
    schema_path: Path,
    codex_executable: str = "codex",
) -> list[str]:
    """Return the fixed non-mutating Codex invocation."""

    return [
        codex_executable,
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--cd",
        str(repo_root),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-",
    ]


def sanitized_codex_environment(values: Mapping[str, str]) -> dict[str, str]:
    """Return the minimum environment required by the isolated Codex subprocess.

    ``HOME`` and ``CODEX_HOME`` are intentionally retained only so the Codex CLI can
    use its controlled local login state; they are not a general credential exception.
    All other variables, including credentials, proxies, and unknown application
    configuration, are dropped by the explicit allowlist.
    """

    result = {
        key: value
        for key, value in values.items()
        if key in _CODEX_ENVIRONMENT_ALLOWLIST
    }
    result["MULTIMODAL_AGENT_PROVIDER_MODE"] = "mock"
    return result


def run_codex_report(
    *,
    bundle_path: Path,
    repo_root: Path,
    output_path: Path,
    schema_path: Path,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 900.0,
    process_runner: Callable[..., Any] = subprocess.run,
) -> CodexAuditReport:
    """Run a credential-isolated Codex analysis and validate its final JSON."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    bundle_path = Path(bundle_path).resolve()
    repo_root = Path(repo_root).resolve()
    output_path = Path(output_path).resolve()
    schema_path = Path(schema_path).resolve()
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        json.dumps(codex_report_json_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    prompt = _codex_prompt(bundle_path)
    process_environment = sanitized_codex_environment(environment or os.environ)
    command = build_codex_command(
        repo_root=repo_root,
        output_path=output_path,
        schema_path=schema_path,
        codex_executable=process_environment.get(
            "ASSISTANT_AGENT_CODEX_EXECUTABLE",
            "codex",
        ),
    )
    result = process_runner(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
        cwd=repo_root,
        env=process_environment,
    )
    if result.returncode != 0:
        stderr = getattr(result, "stderr", "") or "codex exec failed"
        detail = sanitize_error_message(stderr[-1_000:])
        raise RuntimeError(f"Codex runtime audit report failed: {detail}")
    try:
        report = CodexAuditReport.model_validate_json(output_path.read_text(encoding="utf-8"))
        bundle_header = json.loads(bundle_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Codex runtime audit report is not valid structured output.") from exc
    expected_run_id = bundle_header.get("audit_run_id") if isinstance(bundle_header, dict) else None
    if report.audit_run_id != expected_run_id:
        raise RuntimeError("Codex runtime audit report audit_run_id does not match its bundle.")
    return report


def run_daily_codex_report(
    *,
    audit_date: date,
    bundle_path: Path,
    issues_path: Path,
    repo_root: Path,
    output_path: Path,
    schema_path: Path,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 900.0,
    process_runner: Callable[..., Any] = subprocess.run,
) -> DailyCodexAuditReport:
    """Run the isolated daily report contract with prior issue state as input."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    bundle_path = Path(bundle_path).resolve()
    issues_path = Path(issues_path).resolve()
    repo_root = Path(repo_root).resolve()
    output_path = Path(output_path).resolve()
    schema_path = Path(schema_path).resolve()
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        json.dumps(daily_codex_report_json_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    process_environment = sanitized_codex_environment(environment or os.environ)
    command = build_codex_command(
        repo_root=repo_root,
        output_path=output_path,
        schema_path=schema_path,
        codex_executable=process_environment.get(
            "ASSISTANT_AGENT_CODEX_EXECUTABLE",
            "codex",
        ),
    )
    result = process_runner(
        command,
        input=_daily_codex_prompt(
            audit_date=audit_date,
            bundle_path=bundle_path,
            issues_path=issues_path,
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
        cwd=repo_root,
        env=process_environment,
    )
    if result.returncode != 0:
        stderr = getattr(result, "stderr", "") or "codex exec failed"
        detail = sanitize_error_message(stderr[-1_000:])
        raise RuntimeError(f"Codex daily runtime audit report failed: {detail}")
    try:
        report = DailyCodexAuditReport.model_validate_json(
            output_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise RuntimeError(
            "Codex daily runtime audit report is not valid structured output."
        ) from exc
    if report.audit_date != audit_date:
        raise RuntimeError("Codex daily runtime audit report audit_date does not match.")
    return report


def codex_report_json_schema() -> dict[str, Any]:
    """Return the strict object schema required by Codex structured output."""

    schema = CodexAuditReport.model_json_schema()
    _make_object_schemas_strict(schema)
    return schema


def daily_codex_report_json_schema() -> dict[str, Any]:
    """Return the strict structured-output schema for a daily audit report."""

    schema = DailyCodexAuditReport.model_json_schema()
    _make_object_schemas_strict(schema)
    return schema


def _make_object_schemas_strict(value: Any) -> None:
    if isinstance(value, dict):
        value.pop("default", None)
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["additionalProperties"] = False
            value["required"] = list(properties)
        for child in value.values():
            _make_object_schemas_strict(child)
    elif isinstance(value, list):
        for child in value:
            _make_object_schemas_strict(child)


def _codex_prompt(bundle_path: Path) -> str:
    return f"""你是 assistant_agent 的只读运行时审计员。

读取审计 bundle：{bundle_path}

要求：
1. 审计整个 AgentRuntime，不只审计记忆；覆盖质量、工具轨迹、记忆提取/召回和观测完整性。
2. Langfuse trace/observation/Score 是日常主证据；local_fallbacks 仅用于解释缺失导出。
3. judge_pending 和基础设施缺口不是质量失败；execution fact 不是质量 Score。
4. `tool_use` 可作为跨 observation 的报告结论，但不要建议把它伪装成单 observation Score。
5. 只提出有 evidence_refs 的人工修改与验证建议，不修改文件、Langfuse、Mem0、代码或任何外部状态。
6. 不调用 Provider、Tool、网络或其他 agent。最终只输出符合给定 JSON Schema 的对象。
7. production_mutation_allowed 必须为 false，audit_run_id 必须与 bundle 一致。
"""


def _daily_codex_prompt(
    *,
    audit_date: date,
    bundle_path: Path,
    issues_path: Path,
) -> str:
    return f"""你是 assistant_agent 的只读日常运行时审计员。

审计日期：{audit_date.isoformat()}
读取本次只读审计输入：{bundle_path}
读取既有问题状态：{issues_path}

要求：
1. 报告读者是项目维护者，不是另一个 Codex。
2. 先用普通中文解释用户会感受到什么，再说明维护者需要决定什么。
3. 机器 ID 只进入 evidence refs，不在正文堆砌。
4. 代码变化只能标记 code_addressed；没有后续真实 Trace 不得标记 runtime_verified。
5. 同一个 code_addressed 问题不得每天重复完整修改建议。
6. 不得运行测试、修改文件、调用网络、Provider、Tool、Memory 或其他 agent。
7. 只能基于输入中的事实报告；基础设施或证据缺口必须写入 limitations，不能伪造成质量失败。
8. 除输入已有机器证据外，不得声称已运行测试、已部署、已在生产或真实 trace 验证。不得把推测写成事实。
9. production_mutation_allowed 必须为 false，audit_date 必须与审计日期一致。
最终只输出符合给定 JSON Schema 的对象。
"""

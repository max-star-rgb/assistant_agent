"""Isolated Codex report runner helpers."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

from assistant_agent.observability.runtime_audit.models import CodexAuditReport
from assistant_agent.providers.provider_errors import sanitize_error_message


_CREDENTIAL_MARKERS = (
    "API_KEY",
    "APIKEY",
    "AUTHORIZATION",
    "BEARER",
    "COOKIE",
    "CREDENTIAL",
    "LANGFUSE",
    "OPENAI",
    "ANTHROPIC",
    "DASHSCOPE",
    "DEEPSEEK",
    "VOLCENGINE",
    "SECRET",
    "TOKEN",
    "PASSWORD",
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
    """Remove service/provider credentials before starting the report process."""

    result = {
        key: value
        for key, value in values.items()
        if not any(marker in key.upper() for marker in _CREDENTIAL_MARKERS)
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


def codex_report_json_schema() -> dict[str, Any]:
    """Return the strict object schema required by Codex structured output."""

    schema = CodexAuditReport.model_json_schema()
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

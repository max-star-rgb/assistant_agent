"""Prompt-safe evidence adapters for the offline Improvement Lab."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import ValidationError

from assistant_agent.schemas.improvement import (
    ImprovementEvidence,
    ImprovementSourceType,
    ImprovementTargetRef,
)
from assistant_agent.services.trajectory_debug import TrajectoryReplayCase


class EvidenceLoadError(ValueError):
    """Raised when evidence is invalid or unsafe for proposal generation."""


_TRAJECTORY_SYMPTOMS: dict[str, tuple[str, str, str, str]] = {
    "provider_context_overflow": (
        "provider_context_overflow_repeatedly",
        "runtime",
        "context_budget",
        "Provider context overflow stopped the run.",
    ),
    "assistant_loop_limit_reached": (
        "assistant_loop_limit_reached",
        "runtime",
        "assistant_loop",
        "Assistant loop guard stopped the run.",
    ),
    "action_rejected": (
        "tool_validation_rejected_repeatedly",
        "runtime",
        "tool_validation",
        "A governed tool action was rejected.",
    ),
    "tool_retry_exhausted": (
        "tool_retry_exhausted_repeatedly",
        "runtime",
        "tool_execution",
        "Tool retry budget was exhausted.",
    ),
}

_UNSAFE_KEY_MARKERS = (
    "authorization",
    "api_key",
    "token",
    "cookie",
    "raw_",
    "prompt",
    "conversation",
    "memory_content",
    "provider_response",
    "body",
    "base64",
    "media_payload",
    "chain_of_thought",
    "reasoning_content",
    "command_output",
    "system_message",
    "memory_item",
    "user_profile",
    "html",
    "secret",
)
_UNSAFE_VALUE_MARKERS = (
    "authorization:",
    "bearer ",
    "sk-",
    "data:image/",
    "data:video/",
    ";base64,",
    "raw provider response",
    "raw system message",
    "data:text/html",
    "<script",
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|apikey|authorization|bearer|cookie|"
    r"password|token|credential|secret(?:[_-][a-z0-9]+)*)(?:[_-][a-z0-9]+)*"
    r"\b\s*[:=]\s*([^\s,;]+)"
)
_SECRET_PREFIX_RE = re.compile(r"\b(?:sk|pk|qwen|dashscope)-[A-Za-z0-9._-]{4,}\b", re.IGNORECASE)

ALLOWED_SYMPTOM_CODES = {
    "tool_validation_rejected_repeatedly",
    "tool_retry_exhausted_repeatedly",
    "tool_budget_exhausted",
    "assistant_loop_limit_reached",
    "provider_context_overflow_repeatedly",
    "skill_tool_not_selected_in_eval",
    "skill_tool_selected_incorrectly_in_eval",
    "skill_required_input_missing_in_eval",
    "eval_rubric_regression",
    "deterministic_test_regression",
    "latency_budget_regression",
}
_ALLOWED_ATTRIBUTE_KEYS = {
    "rubric_code",
    "score",
    "threshold",
    "module",
    "symbol",
    "run_id",
    "trace_id",
    "event_index",
    "tool_name",
    "error_code",
    "retry_count",
    "budget_ratio",
    "context_usage_ratio",
    "failure_count",
    "latency_ms",
}


def collect_trajectory_evidence(replay: TrajectoryReplayCase) -> list[ImprovementEvidence]:
    """Convert a redacted replay timeline into stable operational evidence."""

    if replay.raw_data_included or not _redaction_is_safe(replay.redaction):
        raise EvidenceLoadError("redacted trajectory replay is required")
    if replay.production_mutation_allowed:
        raise EvidenceLoadError("trajectory replay must forbid production mutation")

    items: list[ImprovementEvidence] = []
    for event in replay.timeline:
        keys = [event.error_code, event.status, event.canonical_event, event.event_type]
        mapping = next((_TRAJECTORY_SYMPTOMS[key] for key in keys if key in _TRAJECTORY_SYMPTOMS), None)
        if mapping is None:
            continue
        symptom_code, target_type, target_ref, summary = mapping
        source_ref = f"trajectory:{replay.trace_id or replay.run_id or 'unknown'}"
        attributes: dict[str, Any] = {
            "run_id": replay.run_id,
            "trace_id": replay.trace_id,
            "event_index": event.index,
        }
        if event.tool_name:
            attributes["tool_name"] = event.tool_name
        if event.error_code:
            attributes["error_code"] = event.error_code
        evidence = _build_evidence(
            source_type="trajectory",
            source_ref=source_ref,
            component=event.node_name,
            target_type=target_type,
            target_ref=target_ref,
            symptom_code=symptom_code,
            summary=summary,
            severity="high" if symptom_code == "assistant_loop_limit_reached" else "medium",
            attributes=attributes,
        )
        issues = validate_evidence_safety(evidence)
        if issues:
            raise EvidenceLoadError(f"unsafe trajectory evidence: {', '.join(issues)}")
        items.append(evidence)
    return deduplicate_evidence(items)


def load_structured_evidence(
    path: Path,
    *,
    source_type: Literal["eval_failure", "test_failure"],
) -> list[ImprovementEvidence]:
    """Load explicit prompt-safe eval or test failure records from JSON."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceLoadError(f"could not load structured evidence: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "improvement_source_records_v1":
        raise EvidenceLoadError("structured evidence schema_version is invalid")
    records = payload.get("records")
    if not isinstance(records, list):
        raise EvidenceLoadError("structured evidence records must be a list")

    items: list[ImprovementEvidence] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise EvidenceLoadError(f"structured evidence record {index} must be an object")
        target_type = record.get("target_type")
        target_ref = record.get("target_ref")
        symptom_code = str(record.get("symptom_code") or "")
        if symptom_code not in ALLOWED_SYMPTOM_CODES:
            raise EvidenceLoadError(f"structured evidence symptom code is not allowed: {symptom_code}")
        if symptom_code.startswith("skill_") and (target_type != "skill" or not target_ref):
            raise EvidenceLoadError("semantic skill evidence requires a skill target hint")
        try:
            evidence = _build_evidence(
                source_type=source_type,
                source_ref=str(record.get("source_ref") or ""),
                component=str(record["component"]) if record.get("component") else None,
                target_type=str(target_type),
                target_ref=str(target_ref or ""),
                symptom_code=symptom_code,
                summary=str(record.get("summary") or ""),
                severity=str(record.get("severity") or "medium"),
                attributes=record.get("attributes") if isinstance(record.get("attributes"), dict) else {},
                occurred_at=record.get("occurred_at"),
            )
        except ValidationError as exc:
            raise EvidenceLoadError(f"structured evidence record {index} is invalid: {exc}") from exc
        issues = validate_evidence_safety(evidence)
        if issues:
            raise EvidenceLoadError(f"unsafe structured evidence record {index}: {', '.join(issues)}")
        items.append(evidence)
    return items


def validate_evidence_safety(evidence: ImprovementEvidence) -> list[str]:
    """Return stable reason codes for content unsafe for proposal access."""

    issues: list[str] = []
    if not evidence.redacted:
        issues.append("evidence_not_redacted")
    _scan_value(evidence.model_dump(mode="json", exclude={"attributes"}), issues)
    for key, value in evidence.attributes.items():
        if key not in _ALLOWED_ATTRIBUTE_KEYS:
            issues.append("evidence_unsafe_field")
            continue
        _scan_value(value, issues, key=key)
    return sorted(set(issues))


def validate_prompt_safe_payload(value: Any) -> list[str]:
    """Scan any provider-visible or persisted payload for unsafe fields/values."""

    issues: list[str] = []
    _scan_value(value, issues)
    return sorted(set(issues))


def deduplicate_evidence(items: Iterable[ImprovementEvidence]) -> list[ImprovementEvidence]:
    """Preserve first-seen evidence order while removing stable-ID duplicates."""

    unique: list[ImprovementEvidence] = []
    seen: set[str] = set()
    for item in items:
        if item.evidence_id in seen:
            continue
        seen.add(item.evidence_id)
        unique.append(item)
    return unique


def _build_evidence(
    *,
    source_type: ImprovementSourceType,
    source_ref: str,
    component: str | None,
    target_type: str,
    target_ref: str,
    symptom_code: str,
    summary: str,
    severity: str,
    attributes: dict[str, Any],
    occurred_at: Any = None,
) -> ImprovementEvidence:
    identity = {
        "source_type": source_type,
        "source_ref": source_ref,
        "component": component,
        "symptom_code": symptom_code,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    return ImprovementEvidence(
        evidence_id=f"evidence_{digest}",
        source_type=source_type,
        source_ref=source_ref,
        occurred_at=occurred_at,
        component=component,
        target_hints=[ImprovementTargetRef(target_type=target_type, target_ref=target_ref)],
        symptom_code=symptom_code,
        summary=summary,
        attributes=attributes,
        severity=severity,
        redacted=True,
    )


def _scan_value(value: Any, issues: list[str], *, key: str = "") -> None:
    lowered_key = key.lower()
    if lowered_key and any(marker in lowered_key for marker in _UNSAFE_KEY_MARKERS):
        issues.append("evidence_unsafe_field")
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            _scan_value(child, issues, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            _scan_value(child, issues)
    elif isinstance(value, str):
        lowered = value.lower()
        if (
            any(marker in lowered for marker in _UNSAFE_VALUE_MARKERS)
            or _SECRET_ASSIGNMENT_RE.search(value)
            or _SECRET_PREFIX_RE.search(value)
        ):
            issues.append("evidence_unsafe_value")


def _redaction_is_safe(redaction: dict[str, Any]) -> bool:
    return all(value is False for key, value in redaction.items() if key.endswith("_included"))

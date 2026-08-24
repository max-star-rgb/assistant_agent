"""Strict contracts for bounded, audited coding-review repair decisions."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence

from assistant_agent.coding.models import (
    MAX_CODING_REVIEW_REPAIR_ATTEMPTS,
    MAX_CODING_REVIEW_REPAIR_FINDINGS,
    MAX_CODING_REVIEW_REPAIR_RESPONSE_CHARS,
    MAX_CODING_REVIEW_REPAIR_RESPONSE_UTF8_BYTES,
    CodingReviewRepairAttempt,
    CodingReviewRepairContext,
    CodingReviewRepairFindingSummary,
    CodingReviewReport,
)


def normalize_review_response(value: object) -> str:
    """Return the sole canonical textual representation accepted for repair."""

    if not isinstance(value, str):
        raise TypeError("coding_review_repair_response_must_be_string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        raise ValueError("coding_review_repair_response_empty")
    if len(normalized) > MAX_CODING_REVIEW_REPAIR_RESPONSE_CHARS:
        raise ValueError("coding_review_repair_response_char_limit_exceeded")
    if len(normalized.encode("utf-8")) > MAX_CODING_REVIEW_REPAIR_RESPONSE_UTF8_BYTES:
        raise ValueError("coding_review_repair_response_utf8_limit_exceeded")
    return normalized


def build_review_repair_context(
    report: CodingReviewReport,
    *,
    review_repair_count: int,
    response: object,
    history: Sequence[CodingReviewRepairAttempt] = (),
) -> CodingReviewRepairContext:
    """Bind one proposed repair to the final review report and budget state."""

    if not isinstance(report, CodingReviewReport):
        raise TypeError("coding_review_repair_report_invalid")
    try:
        canonical_report = CodingReviewReport.model_validate(report.model_dump())
    except Exception as exc:
        raise ValueError("coding_review_repair_report_invalid") from exc
    if type(review_repair_count) is not int or not 0 <= review_repair_count <= MAX_CODING_REVIEW_REPAIR_ATTEMPTS:
        raise ValueError("coding_review_repair_count_invalid")
    if review_repair_count >= MAX_CODING_REVIEW_REPAIR_ATTEMPTS:
        raise ValueError("coding_review_repair_exhausted")
    normalized_history = validate_review_repair_history(history)
    if len(normalized_history) != review_repair_count:
        raise ValueError("coding_review_repair_history_count_mismatch")
    if canonical_report.status != "findings":
        raise ValueError("coding_review_repair_requires_findings")
    report_digest = _required_digest(canonical_report, "report_digest")
    validation_evidence_digest = _required_digest(
        canonical_report, "validation_evidence_digest"
    )
    workspace_diff_digest = _required_digest(canonical_report, "workspace_diff_digest")
    for item in normalized_history:
        if (
            item.report_digest != report_digest
            or item.validation_evidence_digest != validation_evidence_digest
            or item.workspace_diff_digest != workspace_diff_digest
        ):
            raise ValueError("coding_review_repair_history_binding_mismatch")
    normalized_response = normalize_review_response(response)
    return CodingReviewRepairContext(
        attempt=review_repair_count + 1,
        report_digest=report_digest,
        validation_evidence_digest=validation_evidence_digest,
        workspace_diff_digest=workspace_diff_digest,
        response=normalized_response,
        response_digest=_response_digest(normalized_response),
        findings_summary=_findings_summary(canonical_report.findings),
    )


def validate_review_repair_history(
    history: Sequence[CodingReviewRepairAttempt],
) -> tuple[CodingReviewRepairAttempt, ...]:
    """Require an ordered current audit history with at most two attempts."""

    normalized = tuple(CodingReviewRepairAttempt.model_validate(item) for item in history)
    if len(normalized) > MAX_CODING_REVIEW_REPAIR_ATTEMPTS:
        raise ValueError("coding_review_repair_history_limit_exceeded")
    seen: set[int] = set()
    for expected_attempt, item in enumerate(normalized, start=1):
        if item.attempt in seen:
            raise ValueError("coding_review_repair_history_duplicate_attempt")
        if item.attempt != expected_attempt:
            raise ValueError("coding_review_repair_history_non_contiguous")
        seen.add(item.attempt)
    return normalized


def _required_digest(report: object, field: str) -> str:
    value = getattr(report, field, None)
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("coding_review_repair_binding_mismatch")
    return value


def _findings_summary(findings: object) -> tuple[CodingReviewRepairFindingSummary, ...]:
    if not isinstance(findings, (tuple, list)):
        raise ValueError("coding_review_repair_findings_invalid")
    summary: list[CodingReviewRepairFindingSummary] = []
    for finding in findings[:MAX_CODING_REVIEW_REPAIR_FINDINGS]:
        evidence = tuple(getattr(finding, "evidence", ()))
        first = evidence[0] if evidence else None
        title = getattr(finding, "title", None) or getattr(finding, "summary", None)
        remediation = getattr(finding, "remediation", None) or getattr(
            finding, "summary", None
        )
        summary.append(
            CodingReviewRepairFindingSummary(
                finding_id=str(getattr(finding, "finding_id", "")),
                task_id=str(getattr(finding, "task_id", ""))[:64],
                severity=str(getattr(finding, "severity", ""))[:16],
                category=str(getattr(finding, "category", ""))[:64],
                title=str(title or "")[:300],
                path=str(getattr(first, "path", ""))[:512],
                line=int(getattr(first, "line", 0)),
                remediation=str(remediation or "")[:600],
            )
        )
    return tuple(summary)


def _response_digest(response: str) -> str:
    canonical = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_CODING_REVIEW_REPAIR_ATTEMPTS",
    "CodingReviewRepairAttempt",
    "CodingReviewRepairContext",
    "build_review_repair_context",
    "normalize_review_response",
    "validate_review_repair_history",
]

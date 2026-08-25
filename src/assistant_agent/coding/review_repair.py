"""Strict contracts for bounded, audited coding-review repair decisions."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

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
from assistant_agent.coding.review import _canonical_digest, _review_report_digest_payload


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
    source_dirty_paths: object = (),
) -> CodingReviewRepairContext:
    """Bind one proposed repair to the final review report and budget state."""

    canonical_report = canonicalize_review_repair_report(report)
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
    normalized_response = normalize_review_response(response)
    generation = canonical_report.generation
    if type(generation) is not int or generation < 1:
        raise ValueError("coding_review_repair_binding_mismatch")
    snapshot_ref = canonical_report.snapshot_ref
    if not isinstance(snapshot_ref, str):
        raise ValueError("coding_review_repair_binding_mismatch")
    tree_digest = _required_digest(canonical_report, "tree_digest")
    patch_digest = _required_digest(canonical_report, "patch_digest")
    normalized_source_dirty_paths = _canonical_source_dirty_paths(
        source_dirty_paths
    )
    findings_summary = _findings_summary(canonical_report.findings)
    findings_projection_digest = _model_canonical_digest(
        [item.model_dump(mode="json") for item in findings_summary]
    )
    payload = dict(
        previous_history_digest=_review_repair_history_digest_unchecked(
            normalized_history
        ),
        created_at=datetime.now(timezone.utc),
        attempt=review_repair_count + 1,
        workspace_ref=canonical_report.workspace_ref,
        base_commit=canonical_report.base_commit,
        generation=generation,
        snapshot_ref=snapshot_ref,
        snapshot_materialization_schema_version=(
            canonical_report.snapshot_materialization_schema_version
        ),
        snapshot_created_at=canonical_report.snapshot_created_at,
        snapshot_expires_at=canonical_report.snapshot_expires_at,
        tree_digest=tree_digest,
        patch_digest=patch_digest,
        report_digest=report_digest,
        validation_evidence_digest=validation_evidence_digest,
        workspace_diff_digest=workspace_diff_digest,
        source_dirty_paths=normalized_source_dirty_paths,
        response=normalized_response,
        response_digest=review_response_digest(normalized_response),
        findings_summary=findings_summary,
        findings_projection_digest=findings_projection_digest,
    )
    provisional = CodingReviewRepairContext.model_construct(
        **payload,
        context_digest="0" * 64,
    )
    return CodingReviewRepairContext(
        **payload,
        context_digest=_model_canonical_digest(
            provisional.model_dump(mode="json", exclude={"context_digest"})
        ),
    )


def canonicalize_review_repair_report(report: object) -> CodingReviewReport:
    """Strictly revalidate and authenticate one canonical review report."""

    if not isinstance(report, CodingReviewReport):
        raise TypeError("coding_review_repair_report_invalid")
    try:
        canonical_report = CodingReviewReport.model_validate(
            report.model_dump(mode="python", round_trip=True)
        )
    except Exception as exc:
        raise ValueError("coding_review_repair_report_invalid") from exc
    if canonical_report.report_digest != _canonical_digest(
        _review_report_digest_payload(canonical_report)
    ):
        raise ValueError("coding_review_repair_report_digest_invalid")
    return canonical_report


def validate_review_repair_source(
    context: object,
    report: object,
    *,
    workspace_ref: object,
    base_commit: object,
    generation: object,
    source_dirty_paths: object = (),
) -> CodingReviewRepairContext:
    """Bind a repair context to its complete canonical decision source."""

    normalized_context = _canonicalize_review_repair_context(context)
    canonical_report = canonicalize_review_repair_report(report)
    normalized_source_dirty_paths = _canonical_source_dirty_paths(
        source_dirty_paths
    )
    if canonical_report.status != "findings" or (
        normalized_context.workspace_ref != workspace_ref
        or normalized_context.base_commit != base_commit
        or normalized_context.generation != generation
        or normalized_context.workspace_ref != canonical_report.workspace_ref
        or normalized_context.base_commit != canonical_report.base_commit
        or normalized_context.generation != canonical_report.generation
        or normalized_context.snapshot_ref != canonical_report.snapshot_ref
        or normalized_context.snapshot_materialization_schema_version
        != canonical_report.snapshot_materialization_schema_version
        or normalized_context.snapshot_created_at
        != canonical_report.snapshot_created_at
        or normalized_context.snapshot_expires_at
        != canonical_report.snapshot_expires_at
        or normalized_context.tree_digest != canonical_report.tree_digest
        or normalized_context.patch_digest != canonical_report.patch_digest
        or normalized_context.workspace_diff_digest
        != canonical_report.workspace_diff_digest
        or normalized_context.validation_evidence_digest
        != canonical_report.validation_evidence_digest
        or normalized_context.report_digest != canonical_report.report_digest
        or normalized_context.source_dirty_paths != normalized_source_dirty_paths
        or normalized_context.findings_summary
        != _findings_summary(canonical_report.findings)
    ):
        raise ValueError("coding_review_repair_binding_mismatch")
    return normalized_context


def validate_review_repair_history(
    history: Sequence[CodingReviewRepairAttempt],
) -> tuple[CodingReviewRepairAttempt, ...]:
    """Require an ordered current audit history with at most two attempts."""

    normalized = tuple(_canonicalize_review_repair_attempt(item) for item in history)
    if len(normalized) > MAX_CODING_REVIEW_REPAIR_ATTEMPTS:
        raise ValueError("coding_review_repair_history_limit_exceeded")
    seen: set[int] = set()
    for expected_attempt, item in enumerate(normalized, start=1):
        if item.attempt in seen:
            raise ValueError("coding_review_repair_history_duplicate_attempt")
        if item.attempt != expected_attempt:
            raise ValueError("coding_review_repair_history_non_contiguous")
        if item.previous_history_digest != _review_repair_history_digest_unchecked(
            normalized[: expected_attempt - 1]
        ):
            raise ValueError("coding_review_repair_history_mismatch")
        seen.add(item.attempt)
    return normalized


def review_repair_history_digest(
    history: Sequence[CodingReviewRepairAttempt],
) -> str:
    """Return a canonical token binding a decision to its exact audit history."""

    normalized = validate_review_repair_history(history)
    return _review_repair_history_digest_unchecked(normalized)


def _review_repair_history_digest_unchecked(
    history: Sequence[CodingReviewRepairAttempt],
) -> str:
    payload = [item.model_dump(mode="json") for item in history]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_review_repair_checkpoint(
    *,
    review_repair_count: object,
    review_repair_status: object,
    review_repair_context: object,
    review_repair_context_consumed: object,
    review_repair_projection: object,
    history: Sequence[CodingReviewRepairAttempt],
) -> tuple[
    int,
    str | None,
    CodingReviewRepairContext | None,
    tuple[CodingReviewRepairAttempt, ...],
]:
    """Validate the complete bounded repair checkpoint as one state machine."""

    if (
        type(review_repair_count) is not int
        or not 0 <= review_repair_count <= MAX_CODING_REVIEW_REPAIR_ATTEMPTS
    ):
        raise ValueError("coding_review_repair_count_invalid")
    if type(review_repair_context_consumed) is not bool:
        raise ValueError("coding_review_repair_binding_mismatch")
    normalized_history = validate_review_repair_history(history)
    status = review_repair_status

    if status is None:
        if (
            review_repair_count != 0
            or normalized_history
            or review_repair_context is not None
            or review_repair_context_consumed
            or review_repair_projection is not None
        ):
            raise ValueError("coding_review_repair_binding_mismatch")
        return 0, None, None, normalized_history

    if status == "pending":
        if review_repair_context_consumed or review_repair_projection is not None:
            raise ValueError("coding_review_repair_binding_mismatch")
        if review_repair_count == MAX_CODING_REVIEW_REPAIR_ATTEMPTS:
            if (
                review_repair_context is not None
                or len(normalized_history) != review_repair_count
                or normalized_history[-1].outcome
                not in {"pending", "redraft", "proposed"}
            ):
                raise ValueError("coding_review_repair_binding_mismatch")
            return review_repair_count, status, None, normalized_history
        context = _canonicalize_review_repair_context(review_repair_context)
        if (
            context.attempt != review_repair_count + 1
            or len(normalized_history) != review_repair_count + 1
            or not _attempt_matches_context(normalized_history[-1], context)
            or normalized_history[-1].outcome != "pending"
        ):
            raise ValueError("coding_review_repair_binding_mismatch")
        return review_repair_count, status, context, normalized_history

    if status == "active":
        context = _canonicalize_review_repair_context(review_repair_context)
        if (
            review_repair_count < 1
            or context.attempt != review_repair_count
            or len(normalized_history) != review_repair_count
            or not _attempt_matches_context(normalized_history[-1], context)
            or normalized_history[-1].outcome
            not in {"pending", "redraft", "proposed"}
            or (
                not review_repair_context_consumed
                and review_repair_projection is not None
            )
            or (
                review_repair_projection is not None
                and not isinstance(review_repair_projection, Mapping)
            )
        ):
            raise ValueError("coding_review_repair_binding_mismatch")
        return review_repair_count, status, context, normalized_history

    if status == "exhausted":
        if (
            review_repair_count != MAX_CODING_REVIEW_REPAIR_ATTEMPTS
            or len(normalized_history) != review_repair_count
            or review_repair_context is not None
            or review_repair_context_consumed
            or review_repair_projection is not None
        ):
            raise ValueError("coding_review_repair_binding_mismatch")
        return review_repair_count, status, None, normalized_history

    raise ValueError("coding_review_repair_binding_mismatch")


def _attempt_matches_context(
    attempt: CodingReviewRepairAttempt,
    context: CodingReviewRepairContext,
) -> bool:
    return (
        attempt.previous_history_digest == context.previous_history_digest
        and attempt.created_at == context.created_at
        and attempt.attempt == context.attempt
        and attempt.report_digest == context.report_digest
        and attempt.validation_evidence_digest
        == context.validation_evidence_digest
        and attempt.workspace_diff_digest == context.workspace_diff_digest
        and attempt.source_dirty_paths == context.source_dirty_paths
        and attempt.response_digest == context.response_digest
        and attempt.finding_ids
        == tuple(finding.finding_id for finding in context.findings_summary)
        and attempt.context_digest == context.context_digest
    )


def _canonicalize_review_repair_context(
    value: object,
) -> CodingReviewRepairContext:
    payload = (
        value.model_dump(mode="python", round_trip=True)
        if isinstance(value, CodingReviewRepairContext)
        else value
    )
    try:
        return CodingReviewRepairContext.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("coding_review_repair_binding_mismatch") from exc


def _canonicalize_review_repair_attempt(
    value: object,
) -> CodingReviewRepairAttempt:
    payload = (
        value.model_dump(mode="python", round_trip=True)
        if isinstance(value, CodingReviewRepairAttempt)
        else value
    )
    return CodingReviewRepairAttempt.model_validate(payload)


def _required_digest(report: object, field: str) -> str:
    value = getattr(report, field, None)
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("coding_review_repair_binding_mismatch")
    return value


def _canonical_source_dirty_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError("coding_review_repair_binding_mismatch")
    paths: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("coding_review_repair_binding_mismatch")
        parts = item.split("/")
        if (
            not item
            or item != item.strip()
            or item.startswith("/")
            or any(part in {"", ".", "..", ".git"} for part in parts)
            or any(character in item for character in ("\\", "\x00", "\n", "\r"))
            or item in paths
        ):
            raise ValueError("coding_review_repair_binding_mismatch")
        paths.append(item)
    return tuple(sorted(paths))


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


def review_response_digest(response: str) -> str:
    canonical = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _model_canonical_digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_CODING_REVIEW_REPAIR_ATTEMPTS",
    "CodingReviewRepairAttempt",
    "CodingReviewRepairContext",
    "build_review_repair_context",
    "canonicalize_review_repair_report",
    "normalize_review_response",
    "review_response_digest",
    "review_repair_history_digest",
    "validate_review_repair_checkpoint",
    "validate_review_repair_history",
    "validate_review_repair_source",
]

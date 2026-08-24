"""Deterministic contracts and aggregation for coding review workers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from assistant_agent.coding.models import (
    CODING_REVIEW_TASK_SPECS,
    CodingReviewFinding,
    CodingReviewInput,
    CodingReviewReport,
    CodingReviewerResult,
)

REVIEW_TASK_IDS = tuple(CODING_REVIEW_TASK_SPECS)
MAX_REVIEW_RESULT_JSON_CHARS = 16_000
MAX_REVIEW_REPORT_JSON_CHARS = 48_000
_SEVERITY_ORDER = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "info": 4}


def canonicalize_review_report(
    review_input: CodingReviewInput,
    results: Sequence[CodingReviewerResult],
) -> CodingReviewReport:
    """Validate the fixed reviewer inventory and return its canonical report."""

    by_task_id: dict[str, CodingReviewerResult] = {}
    for result in results:
        if result.task_id not in REVIEW_TASK_IDS:
            raise ValueError("coding_review_unknown_task")
        if result.task_id in by_task_id:
            raise ValueError("coding_review_duplicate_task")
        _validate_result(result, review_input)
        by_task_id[result.task_id] = result

    missing = [task_id for task_id in REVIEW_TASK_IDS if task_id not in by_task_id]
    if missing:
        raise ValueError("coding_review_missing_task")

    ordered_results = tuple(by_task_id[task_id] for task_id in REVIEW_TASK_IDS)
    findings = _canonical_findings(ordered_results)
    status = _report_status(ordered_results, findings)
    unsigned = CodingReviewReport(
        status=status,
        workspace_ref=review_input.workspace_ref,
        base_commit=review_input.base_commit,
        patch_digest=review_input.patch_digest,
        workspace_diff_digest=review_input.workspace_diff_digest,
        results=ordered_results,
        findings=findings,
        report_digest="0" * 64,
    )
    payload = unsigned.model_dump(mode="json", exclude={"report_digest"})
    if len(_canonical_json(payload)) > MAX_REVIEW_REPORT_JSON_CHARS:
        raise ValueError("coding_review_report_limit_exceeded")
    return unsigned.model_copy(update={"report_digest": _canonical_digest(payload)})


def _validate_result(result: CodingReviewerResult, review_input: CodingReviewInput) -> None:
    if (
        result.workspace_ref != review_input.workspace_ref
        or result.base_commit != review_input.base_commit
        or result.patch_digest != review_input.patch_digest
        or result.workspace_diff_digest != review_input.workspace_diff_digest
    ):
        raise ValueError("coding_review_binding_mismatch")
    payload = result.model_dump(mode="json", exclude={"output_digest"})
    if result.output_digest != _canonical_digest(payload):
        raise ValueError("coding_review_contract_invalid")
    if len(_canonical_json(payload)) > MAX_REVIEW_RESULT_JSON_CHARS:
        raise ValueError("coding_review_result_limit_exceeded")
    for finding in result.findings:
        if finding.task_id != result.task_id:
            raise ValueError("coding_review_finding_task_mismatch")


def _canonical_findings(
    results: Sequence[CodingReviewerResult],
) -> tuple[CodingReviewFinding, ...]:
    seen: set[tuple[str, str]] = set()
    selected: list[CodingReviewFinding] = []
    for result in results:
        for finding in result.findings:
            evidence_key = _canonical_json(
                [item.model_dump(mode="json") for item in finding.evidence]
            )
            deduplication_key = (evidence_key, finding.semantic_key)
            if deduplication_key in seen:
                continue
            seen.add(deduplication_key)
            selected.append(finding)
    return tuple(sorted(selected, key=_finding_sort_key))


def _finding_sort_key(finding: CodingReviewFinding) -> tuple[int, str, str, int, str]:
    first_evidence = min(
        finding.evidence,
        key=lambda item: (item.path, item.line, item.evidence_digest),
    )
    return (
        _SEVERITY_ORDER[finding.severity],
        finding.task_id,
        first_evidence.path,
        first_evidence.line,
        finding.finding_id,
    )


def _report_status(
    results: Sequence[CodingReviewerResult],
    findings: Sequence[CodingReviewFinding],
) -> str:
    if any(result.status != "succeeded" for result in results):
        return "unavailable"
    return "findings" if findings else "clean"


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "MAX_REVIEW_REPORT_JSON_CHARS",
    "MAX_REVIEW_RESULT_JSON_CHARS",
    "REVIEW_TASK_IDS",
    "canonicalize_review_report",
]

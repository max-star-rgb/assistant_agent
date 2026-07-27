"""Deterministic opportunity detection over prompt-safe evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json

from assistant_agent.improvement.models import (
    ImprovementEvidence,
    ImprovementOpportunity,
    ImprovementTargetType,
)


_SKILL_SEMANTIC_PATTERNS = {
    "skill_tool_not_selected_in_eval",
    "skill_tool_selected_incorrectly_in_eval",
    "skill_required_input_missing_in_eval",
}
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def detect_opportunities(
    evidence: Sequence[ImprovementEvidence],
    *,
    target_type: ImprovementTargetType | None = None,
    now: datetime | None = None,
    max_age_days: int = 30,
) -> list[ImprovementOpportunity]:
    """Group evidence by concrete target and apply local eligibility rules."""

    grouped: dict[tuple[str, str, str], list[ImprovementEvidence]] = defaultdict(list)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)
    for item in evidence:
        if item.occurred_at is not None and item.occurred_at < cutoff:
            continue
        for hint in item.target_hints:
            if target_type is not None and hint.target_type != target_type:
                continue
            grouped[(hint.target_type, hint.target_ref, item.symptom_code)].append(item)

    opportunities = [
        _build_opportunity(key, items)
        for key, items in sorted(grouped.items(), key=lambda entry: entry[0])
    ]
    return opportunities


def _build_opportunity(
    key: tuple[str, str, str],
    items: list[ImprovementEvidence],
) -> ImprovementOpportunity:
    target_type, target_ref, pattern_code = key
    unique_by_source: dict[str, ImprovementEvidence] = {}
    for item in sorted(items, key=lambda value: (value.source_ref, value.evidence_id)):
        unique_by_source.setdefault(item.source_ref, item)
    unique = list(unique_by_source.values())
    source_types = {item.source_type for item in unique}
    severity = max((item.severity for item in unique), key=_SEVERITY_ORDER.__getitem__)

    blocked: list[str] = []
    severe_regression = any(
        item.severity == "high" and item.source_type in {"eval_failure", "test_failure"}
        for item in unique
    )
    if len(unique) < 2 and not severe_regression:
        blocked.append("independent_evidence_required")
    if target_type == "skill" and pattern_code in _SKILL_SEMANTIC_PATTERNS:
        if "eval_failure" not in source_types:
            blocked.append("skill_eval_evidence_required")
    if target_type == "code" and not any(
        item.attributes.get("module") or item.attributes.get("symbol") for item in unique
    ):
        blocked.append("concrete_code_location_required")

    identity = {
        "target_type": target_type,
        "target_ref": target_ref,
        "pattern_code": pattern_code,
        "evidence_refs": sorted(item.evidence_id for item in unique),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:20]
    confidence = _confidence(
        recurrence_count=len(unique),
        source_type_count=len(source_types),
        high_severity=severity == "high",
        concrete_target=bool(target_ref),
    )
    return ImprovementOpportunity(
        opportunity_id=f"opportunity_{digest}",
        target_type=target_type,
        target_ref=target_ref,
        evidence_refs=sorted(item.evidence_id for item in unique),
        pattern_code=pattern_code,
        problem_statement=(
            f"Observed {pattern_code} for {target_type} target {target_ref} "
            f"across {len(unique)} independent source(s)."
        ),
        recurrence_count=len(unique),
        source_type_count=len(source_types),
        impact=severity,
        confidence=confidence,
        status="insufficient_evidence" if blocked else "ready_for_proposal",
        blocked_reasons=blocked,
    )


def _confidence(
    *,
    recurrence_count: int,
    source_type_count: int,
    high_severity: bool,
    concrete_target: bool,
) -> float:
    score = 0.35
    if recurrence_count >= 2:
        score += 0.2
    if source_type_count >= 2:
        score += 0.1
    if high_severity:
        score += 0.2
    if concrete_target:
        score += 0.15
    return round(min(score, 1.0), 4)

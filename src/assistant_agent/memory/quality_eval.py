"""Offline memory write-quality eval helpers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.memory.write_policy import (
    MemoryPromotionKind,
    MemoryWriteDecisionSensitivity,
    MemoryWriteDestination,
    MemoryWritePolicy,
    build_memory_promotion_candidate,
)
from assistant_agent.schemas.memory import MemoryScope, MemoryType
from assistant_agent.services.provider_errors import sanitize_error_message


MemoryQualityAction = Literal["write", "reject", "confirm"]
MemoryQualityCaseKind = Literal["explicit", "promotion"]


class MemoryQualityEvalCase(BaseModel):
    """One deterministic eval case for memory write-policy quality."""

    id: str
    case_kind: MemoryQualityCaseKind = "explicit"
    text: str = ""
    content: dict[str, Any] = Field(default_factory=dict)
    scope: MemoryScope | None = None
    summary: str = ""
    memory_type: MemoryType = "task"
    kind: MemoryPromotionKind = "episodic_memory"
    tags: list[str] = Field(default_factory=list)
    source: str = "memory_quality_eval"
    user_intent_explicit: bool = False
    rejected_reason: str | None = None
    policy: dict[str, Any] = Field(default_factory=dict)
    expected_action: MemoryQualityAction
    expected_destination: MemoryWriteDestination | None = None
    expected_sensitivity: MemoryWriteDecisionSensitivity | None = None
    expected_reason_contains: str | None = None


class MemoryQualityEvalResult(BaseModel):
    """Policy decision quality result for one eval case."""

    id: str
    passed: bool
    case_kind: MemoryQualityCaseKind
    expected_action: MemoryQualityAction
    actual_action: MemoryQualityAction
    expected_destination: MemoryWriteDestination | None = None
    actual_destination: MemoryWriteDestination
    expected_sensitivity: MemoryWriteDecisionSensitivity | None = None
    actual_sensitivity: MemoryWriteDecisionSensitivity
    expected_reason_contains: str | None = None
    actual_reason: str
    feedback: dict[str, Any]


def evaluate_memory_quality_case(payload: dict[str, Any]) -> MemoryQualityEvalResult:
    """Evaluate one memory write-quality case without network or model calls."""

    case = MemoryQualityEvalCase.model_validate(payload)
    policy = MemoryWritePolicy.model_validate(case.policy) if case.policy else MemoryWritePolicy()
    if case.case_kind == "promotion":
        candidate = build_memory_promotion_candidate(
            user_id="u1",
            session_id="s1",
            summary=case.summary or case.text,
            memory_type=case.memory_type,
            kind=case.kind,
            content=case.content,
            tags=case.tags,
            source=case.source,
            user_intent_explicit=case.user_intent_explicit,
            rejected_reason=case.rejected_reason,
        )
        decision = policy.evaluate_promotion_candidate(candidate)
    else:
        decision = policy.evaluate_explicit_save(
            text=case.text,
            content=case.content,
            scope=case.scope,
        )
    actual_action = _action_for_decision(decision_allowed=decision.allowed, confirm=decision.require_user_confirmation)
    actual_destination = _quality_destination(decision)
    reason = sanitize_error_message(decision.reason)
    destination_ok = case.expected_destination is None or case.expected_destination == actual_destination
    sensitivity_ok = case.expected_sensitivity is None or case.expected_sensitivity == decision.sensitivity
    reason_ok = (
        case.expected_reason_contains is None
        or case.expected_reason_contains in reason
    )
    passed = (
        actual_action == case.expected_action
        and destination_ok
        and sensitivity_ok
        and reason_ok
    )
    feedback = {
        "action": actual_action,
        "allowed": decision.allowed,
        "destination": actual_destination,
        "requires_confirmation": decision.require_user_confirmation,
        "sensitivity": decision.sensitivity,
    }
    return MemoryQualityEvalResult(
        id=case.id,
        passed=passed,
        case_kind=case.case_kind,
        expected_action=case.expected_action,
        actual_action=actual_action,
        expected_destination=case.expected_destination,
        actual_destination=actual_destination,
        expected_sensitivity=case.expected_sensitivity,
        actual_sensitivity=decision.sensitivity,
        expected_reason_contains=case.expected_reason_contains,
        actual_reason=reason,
        feedback=feedback,
    )


def summarize_memory_quality_eval(results: list[MemoryQualityEvalResult]) -> dict[str, Any]:
    """Aggregate memory write-quality eval metrics."""

    expected_writes = [result for result in results if result.expected_action == "write"]
    actual_writes = [result for result in results if result.actual_action == "write"]
    expected_rejects = [result for result in results if result.expected_action == "reject"]
    expected_confirmations = [result for result in results if result.expected_action == "confirm"]
    expected_secret_rejections = [
        result
        for result in results
        if result.expected_action == "reject" and result.expected_sensitivity == "secret"
    ]
    false_writes = [
        result
        for result in results
        if result.expected_action != "write" and result.actual_action == "write"
    ]
    return {
        "total": len(results),
        "passed": sum(1 for result in results if result.passed),
        "failed": sum(1 for result in results if not result.passed),
        "action_accuracy": _rate([result.actual_action == result.expected_action for result in results]),
        "write_precision": _rate([result.expected_action == "write" for result in actual_writes], empty_default=1.0),
        "write_recall": _rate([result.actual_action == "write" for result in expected_writes], empty_default=1.0),
        "reject_recall": _rate([result.actual_action == "reject" for result in expected_rejects], empty_default=1.0),
        "confirmation_recall": _rate(
            [result.actual_action == "confirm" for result in expected_confirmations],
            empty_default=1.0,
        ),
        "secret_rejection_rate": _rate(
            [
                result.actual_action == "reject" and result.actual_sensitivity == "secret"
                for result in expected_secret_rejections
            ],
            empty_default=1.0,
        ),
        "false_write_rate": len(false_writes) / len(results) if results else 0.0,
    }


def summarize_memory_quality_eval_dicts(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize serialized quality eval results from the generic eval runner."""

    return summarize_memory_quality_eval(
        [MemoryQualityEvalResult.model_validate(result) for result in results]
    )


def _action_for_decision(*, decision_allowed: bool, confirm: bool) -> MemoryQualityAction:
    if decision_allowed:
        return "write"
    if confirm:
        return "confirm"
    return "reject"


def _quality_destination(decision) -> MemoryWriteDestination:
    if decision.require_user_confirmation:
        destination = decision.redacted_payload.get("destination")
        if destination in {
            "reject",
            "session_summary",
            "task_checkpoint",
            "project_memory",
            "user_profile",
            "video_memory",
            "product_memory",
        }:
            return destination
    return decision.destination


def _rate(values: list[bool], *, empty_default: float = 0.0) -> float:
    return sum(1 for value in values if value) / len(values) if values else empty_default

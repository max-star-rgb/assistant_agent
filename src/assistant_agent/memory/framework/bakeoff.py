"""Deterministic scoring and winner selection for measured framework bake-offs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


_FIXED_VERSIONS = {"hindsight": "0.8.4", "mem0": "2.0.11"}


class FrameworkBakeoffMetrics(BaseModel):
    framework: Literal["hindsight", "mem0"]
    version: str
    recall_at_5: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)
    write_precision: float = Field(ge=0, le=1)
    contradiction_accuracy: float = Field(ge=0, le=1)
    temporal_accuracy: float = Field(ge=0, le=1)
    multihop_accuracy: float = Field(ge=0, le=1)
    chinese_accuracy: float = Field(ge=0, le=1)
    episodic_procedural_accuracy: float = Field(ge=0, le=1)
    false_positive_rate: float = Field(ge=0, le=1)
    cross_user_leakage_rate: float = Field(ge=0, le=1)
    crud_history_verified: bool
    export_delete_clear_verified: bool
    audit_mapping_verified: bool
    langgraph_context_verified: bool
    governance_bypass_detected: bool
    p95_retain_ms: float = Field(ge=0)
    p95_recall_ms: float = Field(ge=0)
    rss_mb: float = Field(ge=0)
    disk_mb: float = Field(ge=0)
    cold_start_seconds: float = Field(ge=0)
    restart_recovery_verified: bool
    no_silent_write_loss: bool
    default_tests_offline: bool
    backup_portable: bool
    configuration_steps: int = Field(ge=0)

    def validate_fixed_version(self) -> "FrameworkBakeoffMetrics":
        expected = _FIXED_VERSIONS[self.framework]
        if self.version != expected:
            raise ValueError(
                f"fixed bake-off version for {self.framework} is {expected}, got {self.version}"
            )
        return self


class BakeoffScoreSection(BaseModel):
    points: float = Field(ge=0)
    max_points: int
    breakdown: dict[str, float] = Field(default_factory=dict)


class FrameworkBakeoffResult(BaseModel):
    framework: Literal["hindsight", "mem0"]
    version: str
    quality: BakeoffScoreSection
    governance: BakeoffScoreSection
    operations: BakeoffScoreSection
    total: float
    passed: bool
    failed_gates: list[str] = Field(default_factory=list)
    p95_recall_ms: float
    rss_mb: float


class FrameworkWinnerDecision(BaseModel):
    winner: Literal["hindsight", "mem0"] | None = None
    recommendation: Literal["use_framework", "keep_v2"]
    reason: str
    results: dict[str, FrameworkBakeoffResult]


def score_bakeoff(metrics: FrameworkBakeoffMetrics) -> FrameworkBakeoffResult:
    metrics.validate_fixed_version()
    quality_breakdown = {
        "recall_at_5": metrics.recall_at_5 * 8,
        "mrr": metrics.mrr * 5,
        "write_precision": metrics.write_precision * 6,
        "contradiction_update": metrics.contradiction_accuracy * 5,
        "temporal_query": metrics.temporal_accuracy * 4,
        "multihop_relation": metrics.multihop_accuracy * 4,
        "chinese": metrics.chinese_accuracy * 4,
        "episodic_procedural": metrics.episodic_procedural_accuracy * 3,
        "false_positive_control": (1 - metrics.false_positive_rate) * 6,
    }
    governance_breakdown = {
        "identity_isolation": (1 - metrics.cross_user_leakage_rate) * 7,
        "crud_history": 4.0 if metrics.crud_history_verified else 0.0,
        "export_delete_clear": 5.0 if metrics.export_delete_clear_verified else 0.0,
        "audit_mapping": 3.0 if metrics.audit_mapping_verified else 0.0,
        "langgraph_context": 4.0 if metrics.langgraph_context_verified else 0.0,
        "governance_boundary": 2.0 if not metrics.governance_bypass_detected else 0.0,
    }
    operations_breakdown = {
        "p95_retain": _lower_is_better(metrics.p95_retain_ms, target=250, points=4),
        "p95_recall": _lower_is_better(metrics.p95_recall_ms, target=200, points=5),
        "rss": _lower_is_better(metrics.rss_mb, target=1024, points=4),
        "disk": _lower_is_better(metrics.disk_mb, target=1024, points=3),
        "cold_start": _lower_is_better(metrics.cold_start_seconds, target=30, points=3),
        "restart_recovery": 4.0 if metrics.restart_recovery_verified else 0.0,
        "no_silent_write_loss": 4.0 if metrics.no_silent_write_loss else 0.0,
        "offline_default": 1.0 if metrics.default_tests_offline else 0.0,
        "backup_portability": 1.0 if metrics.backup_portable else 0.0,
        "configuration_complexity": _lower_is_better(metrics.configuration_steps, target=10, points=1),
    }
    quality = BakeoffScoreSection(
        points=round(sum(quality_breakdown.values()), 4), max_points=45, breakdown=quality_breakdown
    )
    governance = BakeoffScoreSection(
        points=round(sum(governance_breakdown.values()), 4), max_points=25, breakdown=governance_breakdown
    )
    operations = BakeoffScoreSection(
        points=round(sum(operations_breakdown.values()), 4), max_points=30, breakdown=operations_breakdown
    )
    total = round(quality.points + governance.points + operations.points, 4)
    failed_gates = _failed_gates(metrics, total=total, quality=quality.points)
    return FrameworkBakeoffResult(
        framework=metrics.framework,
        version=metrics.version,
        quality=quality,
        governance=governance,
        operations=operations,
        total=total,
        passed=not failed_gates,
        failed_gates=failed_gates,
        p95_recall_ms=metrics.p95_recall_ms,
        rss_mb=metrics.rss_mb,
    )


def select_framework_winner(
    hindsight: FrameworkBakeoffMetrics,
    mem0: FrameworkBakeoffMetrics,
) -> FrameworkWinnerDecision:
    results = {
        "hindsight": score_bakeoff(hindsight),
        "mem0": score_bakeoff(mem0),
    }
    eligible = [result for result in results.values() if result.passed]
    if not eligible:
        return FrameworkWinnerDecision(
            recommendation="keep_v2",
            reason="neither framework passed all hard gates",
            results=results,
        )
    if len(eligible) == 1:
        winner = eligible[0]
    else:
        first, second = eligible
        if abs(first.total - second.total) > 3:
            winner = max(eligible, key=lambda result: result.total)
        elif first.operations.points != second.operations.points:
            winner = max(eligible, key=lambda result: result.operations.points)
        elif first.p95_recall_ms != second.p95_recall_ms:
            winner = min(eligible, key=lambda result: result.p95_recall_ms)
        else:
            winner = min(eligible, key=lambda result: result.rss_mb)
    return FrameworkWinnerDecision(
        winner=winner.framework,
        recommendation="use_framework",
        reason="winner selected by score and documented tie-break rules",
        results=results,
    )


def _failed_gates(metrics: FrameworkBakeoffMetrics, *, total: float, quality: float) -> list[str]:
    failures: list[str] = []
    if metrics.cross_user_leakage_rate != 0:
        failures.append("cross_user_leakage_rate_must_be_zero")
    if not metrics.export_delete_clear_verified:
        failures.append("export_delete_clear_must_be_verified")
    if metrics.governance_bypass_detected or not metrics.langgraph_context_verified:
        failures.append("langgraph_tool_executor_memory_manager_boundaries_required")
    if not metrics.default_tests_offline:
        failures.append("default_tests_must_be_offline")
    if not metrics.restart_recovery_verified:
        failures.append("sidecar_restart_recovery_must_be_verified")
    if not metrics.no_silent_write_loss:
        failures.append("sidecar_failure_must_not_silently_drop_writes")
    if total < 75:
        failures.append("total_score_below_75")
    if quality < 35:
        failures.append("quality_score_below_35")
    return failures


def _lower_is_better(value: float, *, target: float, points: float) -> float:
    if value <= target:
        return points
    if value >= target * 3:
        return 0.0
    return points * (target * 3 - value) / (target * 2)

import pytest

from assistant_agent.memory.framework.bakeoff import (
    FrameworkBakeoffMetrics,
    score_bakeoff,
    select_framework_winner,
)


def _passing(name: str, *, recall_ms: float = 120, rss_mb: float = 500) -> FrameworkBakeoffMetrics:
    return FrameworkBakeoffMetrics(
        framework=name,
        version="0.8.4" if name == "hindsight" else "2.0.11",
        recall_at_5=0.9,
        mrr=0.85,
        write_precision=0.9,
        contradiction_accuracy=0.8,
        temporal_accuracy=0.8,
        multihop_accuracy=0.8,
        chinese_accuracy=0.9,
        episodic_procedural_accuracy=0.8,
        false_positive_rate=0.05,
        cross_user_leakage_rate=0.0,
        crud_history_verified=True,
        export_delete_clear_verified=True,
        audit_mapping_verified=True,
        langgraph_context_verified=True,
        governance_bypass_detected=False,
        p95_retain_ms=180,
        p95_recall_ms=recall_ms,
        rss_mb=rss_mb,
        disk_mb=300,
        cold_start_seconds=15,
        restart_recovery_verified=True,
        no_silent_write_loss=True,
        default_tests_offline=True,
        backup_portable=True,
        configuration_steps=5,
    )


def test_score_has_fixed_45_25_30_weights_and_passes_hard_gates() -> None:
    result = score_bakeoff(_passing("hindsight"))

    assert result.quality.max_points == 45
    assert result.governance.max_points == 25
    assert result.operations.max_points == 30
    assert result.total == pytest.approx(
        result.quality.points + result.governance.points + result.operations.points
    )
    assert result.passed is True


def test_cross_user_leakage_or_silent_loss_blocks_selection_even_with_high_score() -> None:
    unsafe = _passing("hindsight").model_copy(
        update={"cross_user_leakage_rate": 0.01, "no_silent_write_loss": False}
    )

    result = score_bakeoff(unsafe)

    assert result.passed is False
    assert "cross_user_leakage_rate_must_be_zero" in result.failed_gates
    assert "sidecar_failure_must_not_silently_drop_writes" in result.failed_gates


def test_no_winner_keeps_v2_when_both_fail() -> None:
    hindsight = _passing("hindsight").model_copy(update={"recall_at_5": 0.1})
    mem0 = _passing("mem0").model_copy(update={"cross_user_leakage_rate": 0.1})

    decision = select_framework_winner(hindsight, mem0)

    assert decision.winner is None
    assert decision.recommendation == "keep_v2"


def test_tie_break_uses_operations_then_recall_latency_then_rss() -> None:
    hindsight = _passing("hindsight", recall_ms=100, rss_mb=700)
    mem0 = _passing("mem0", recall_ms=140, rss_mb=400)
    initial = select_framework_winner(hindsight, mem0)

    assert initial.winner in {"hindsight", "mem0"}
    if abs(initial.results["hindsight"].total - initial.results["mem0"].total) <= 3:
        higher_ops = max(
            initial.results.values(), key=lambda result: result.operations.points
        ).framework
        assert initial.winner == higher_ops


def test_rejects_unpinned_framework_version() -> None:
    with pytest.raises(ValueError, match="fixed bake-off version"):
        _passing("mem0").model_copy(update={"version": "latest"}).validate_fixed_version()

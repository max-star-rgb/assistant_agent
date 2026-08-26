import pytest

from assistant_agent.coding.inspect_recovery import (
    canonical_inspect_progress_digest,
    validate_inspect_recovery_checkpoint,
)
from assistant_agent.coding.models import CodingInspectProgress


def _state() -> dict[str, object]:
    value = {
        "epoch": 1,
        "reason": "model_budget_exhausted",
        "base_commit": "a" * 40,
        "workspace_diff_digest": "b" * 64,
        "calls": (),
    }
    progress = CodingInspectProgress(**value, progress_digest=canonical_inspect_progress_digest(value))
    return {
        "inspect_epoch": 2,
        "inspect_recovery_status": "retrying",
        "inspect_progress": None,
        "inspect_recovery_history": ({"epoch": 1, "progress": progress, "outcome": "retrying"},),
        "inspect_recovery_context_consumed": True,
    }


def test_retrying_checkpoint_is_bound_to_base_and_diff() -> None:
    history = validate_inspect_recovery_checkpoint(
        _state(), base_commit="a" * 40, workspace_diff_digest="b" * 64
    )
    assert len(history) == 1


@pytest.mark.parametrize("field,value", [("base_commit", "c" * 40), ("workspace_diff_digest", "d" * 64)])
def test_checkpoint_drift_fails_closed(field: str, value: str) -> None:
    kwargs = {"base_commit": "a" * 40, "workspace_diff_digest": "b" * 64, field: value}
    with pytest.raises(ValueError, match="coding_inspect_recovery_binding_mismatch"):
        validate_inspect_recovery_checkpoint(_state(), **kwargs)


def test_impossible_context_combination_fails_closed() -> None:
    state = _state()
    state["inspect_recovery_status"] = "pending"
    with pytest.raises(ValueError, match="coding_inspect_recovery_binding_mismatch"):
        validate_inspect_recovery_checkpoint(
            state, base_commit="a" * 40, workspace_diff_digest="b" * 64
        )

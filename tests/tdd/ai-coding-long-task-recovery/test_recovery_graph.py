from assistant_agent.coding.inspect_recovery import evaluate_inspect_recovery
from assistant_agent.coding.models import CodingInspectProgress


def _progress(epoch: int, call_digest: str) -> CodingInspectProgress:
    from assistant_agent.coding.inspect_recovery import canonical_inspect_progress_digest

    value = {
        "epoch": epoch,
        "reason": "tool_budget_exhausted",
        "base_commit": "a" * 40,
        "workspace_diff_digest": "b" * 64,
        "calls": ({
            "tool_name": "coding_repo_read_file",
            "arguments_digest": call_digest,
            "result_digest": "d" * 64,
            "relative_paths": (f"src/{call_digest[0]}.py",),
        },),
    }
    return CodingInspectProgress(**value, progress_digest=canonical_inspect_progress_digest(value))


def test_first_budget_exhaustion_schedules_second_epoch() -> None:
    progress = _progress(1, "c" * 64)
    outcome = evaluate_inspect_recovery(progress, ())
    assert outcome.status == "retrying"
    assert outcome.next_epoch == 2


def test_subset_progress_terminates_without_another_epoch() -> None:
    first = _progress(1, "c" * 64)
    history = ({"epoch": 1, "progress": first, "outcome": "retrying"},)
    second = first.model_copy(update={"epoch": 2})
    outcome = evaluate_inspect_recovery(second, history)
    assert outcome.status == "no_progress"
    assert outcome.error_code == "coding_inspect_no_progress"


def test_third_progressing_epoch_exhausts_recovery() -> None:
    first = _progress(1, "a" * 64)
    second = _progress(2, "b" * 64)
    third = _progress(3, "c" * 64)
    history = (
        {"epoch": 1, "progress": first, "outcome": "completed"},
        {"epoch": 2, "progress": second, "outcome": "retrying"},
    )
    outcome = evaluate_inspect_recovery(third, history)
    assert outcome.status == "exhausted"
    assert outcome.error_code == "coding_inspect_recovery_exhausted"


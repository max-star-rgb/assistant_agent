import pytest
from pydantic import ValidationError

from assistant_agent.coding.inspect_recovery import (
    canonical_inspect_progress_digest,
    validate_inspect_recovery_history,
)
from assistant_agent.coding.models import (
    CodingInspectCallEvidence,
    CodingInspectProgress,
    CodingInspectRecoveryAttempt,
)


BASE = "a" * 40
DIFF = "b" * 64


def _call(path: str = "src/calc.py") -> CodingInspectCallEvidence:
    return CodingInspectCallEvidence(
        tool_name="coding_repo_read_file",
        arguments_digest="c" * 64,
        result_digest="d" * 64,
        relative_paths=(path,),
    )


def _progress(epoch: int = 1) -> CodingInspectProgress:
    value = {
        "epoch": epoch,
        "reason": "tool_budget_exhausted",
        "base_commit": BASE,
        "workspace_diff_digest": DIFF,
        "calls": (_call(),),
    }
    return CodingInspectProgress(
        **value,
        progress_digest=canonical_inspect_progress_digest(value),
    )


def test_call_evidence_rejects_host_and_oversize_paths() -> None:
    assert _call().relative_paths == ("src/calc.py",)
    for path in ("/tmp/secret", "../escape.py", "src/../../escape.py"):
        with pytest.raises(ValidationError):
            _call(path)
    with pytest.raises(ValidationError):
        CodingInspectCallEvidence(
            tool_name="coding_repo_read_file",
            arguments_digest="c" * 64,
            result_digest="d" * 64,
            relative_paths=tuple(f"src/{index}.py" for index in range(33)),
        )


def test_progress_is_strict_and_digest_is_epoch_independent() -> None:
    first = _progress(1)
    second = _progress(2)
    assert first.progress_digest == second.progress_digest
    with pytest.raises(ValidationError):
        CodingInspectProgress(**{**first.model_dump(), "unknown": True})
    with pytest.raises(ValidationError):
        CodingInspectProgress(**{**first.model_dump(), "progress_digest": "nope"})


def test_history_rejects_epoch_gaps_and_duplicate_progress() -> None:
    first = _progress(1)
    second = _progress(2)
    valid = CodingInspectRecoveryAttempt(
        epoch=1, progress=first, outcome="retrying"
    )
    assert validate_inspect_recovery_history((valid,)) == (valid,)
    with pytest.raises(ValueError):
        validate_inspect_recovery_history(
            (
                valid.model_copy(update={"outcome": "completed"}),
                CodingInspectRecoveryAttempt(
                    epoch=2, progress=second, outcome="retrying"
                ),
            )
        )


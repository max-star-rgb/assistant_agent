"""Deterministic graders for the native coding behavior system evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import threading
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.evaluation.coding_behavior import (
    SCHEMA_VERSION,
    TRUSTED_GRADER_IDS,
    CodingBehaviorCase,
    CodingBehaviorCheckResult,
    CodingBehaviorError,
)
from evals.system.ai_coding_behavior.fixtures import (
    CodingBehaviorFixture,
    governed_git_environment,
)


_MAX_GIT_OUTPUT_BYTES = 65_536
_MAX_COMMAND_OUTPUT_BYTES = 32_768
_COMMAND_TIMEOUT_SECONDS = 20


@dataclass(frozen=True, slots=True)
class CodingBehaviorGradeInput:
    fixture: CodingBehaviorFixture
    terminal_status: str
    interrupt_kinds: tuple[str, ...]
    elapsed_ms: int
    interrupt_count: int
    max_interrupts: int
    evidence_size_bytes: int
    max_evidence_size_bytes: int
    validation_tree_digest: str
    review_tree_digest: str
    integration_tree_digest: str
    final_commit: str

    def __post_init__(self) -> None:
        bounded = (
            self.elapsed_ms,
            self.interrupt_count,
            self.max_interrupts,
            self.evidence_size_bytes,
            self.max_evidence_size_bytes,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in bounded):
            raise ValueError("grader bounds must be non-negative integers")


class CodingBehaviorCommandEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    returncode: int | None
    stdout_digest: str
    stderr_digest: str
    error_category: Literal["none", "failed", "timed_out", "output_limit", "spawn_failed"]


class CodingBehaviorGradingReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    status: Literal["passed", "failed"]
    checks: tuple[CodingBehaviorCheckResult, ...]
    changed_paths: tuple[str, ...]
    command_evidence: CodingBehaviorCommandEvidence | None = None


_HELD_OUT_COMMANDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "single-file-logic-bug-v1": (
            "-I", "-c",
            "import runpy; m=runpy.run_path('src/range_check.py'); assert m['contains'](3, 1, 3); assert not m['contains'](4, 1, 3)",
        ),
        "multi-file-interface-v1": (
            "-I", "-c",
            "import sys; sys.path.insert(0, 'src'); from client import render_user; assert render_user({'first_name':'Ada','last_name':'Lovelace'}) == 'Lovelace, Ada'",
        ),
        "regression-test-required-v1": (
            "-I", "-c",
            "import runpy; m=runpy.run_path('src/escaping.py'); assert m['escape_html']('<a&b>') == '&lt;a&amp;b&gt;'; p=open('tests/test_escaping.py', encoding='utf-8').read(); assert '&' in p",
        ),
        "scope-discipline-v1": (
            "-I", "-c",
            "import runpy; m=runpy.run_path('src/total.py'); assert m['calculate_total']([2, 3, 5]) == 10; assert m['calculate_total']([]) == 0",
        ),
    }
)


def _error(message: str) -> CodingBehaviorError:
    return CodingBehaviorError(
        schema_version=SCHEMA_VERSION,
        code="coding_eval_grader_failed",
        message=message,
    )


def _check(check_id: str, passed: bool, message: str) -> CodingBehaviorCheckResult:
    return CodingBehaviorCheckResult(
        schema_version=SCHEMA_VERSION,
        check_id=check_id,  # type: ignore[arg-type]
        status="passed" if passed else "failed",
        error=None if passed else _error(message),
    )


def _git(repository: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            env=governed_git_environment(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("grader Git operation failed") from exc
    if len(completed.stdout) > _MAX_GIT_OUTPUT_BYTES or len(completed.stderr) > _MAX_GIT_OUTPUT_BYTES:
        raise ValueError("grader Git output exceeded its bound")
    if completed.returncode != 0:
        raise ValueError("grader Git operation failed")
    return completed.stdout


def _actual_changed_paths(value: CodingBehaviorGradeInput) -> tuple[str, ...]:
    output = _git(
        value.fixture.repository,
        "diff",
        "--name-only",
        "-z",
        f"{value.fixture.base_commit}..{value.final_commit}",
        "--",
    )
    paths = tuple(sorted(item.decode("utf-8") for item in output.split(b"\0") if item))
    for item in paths:
        path = PurePosixPath(item)
        if path.is_absolute() or path.as_posix() != item or any(part in {"", ".", "..", ".git"} for part in path.parts):
            raise ValueError("Git returned a noncanonical changed path")
        entry = _git(
            value.fixture.repository,
            "ls-tree",
            value.final_commit,
            "--",
            item,
        ).decode("utf-8")
        if entry.startswith("120000 "):
            raise ValueError("changed path is a symbolic link")
    return paths


def _digest_pipe(
    pipe: object,
    result: dict[str, object],
    key: str,
    process: subprocess.Popen[bytes],
) -> None:
    digest = sha256()
    total = 0
    exceeded = False
    stream = pipe
    try:
        while True:
            chunk = stream.read(4096)  # type: ignore[attr-defined]
            if not chunk:
                break
            total += len(chunk)
            if total <= _MAX_COMMAND_OUTPUT_BYTES:
                digest.update(chunk)
            else:
                exceeded = True
                process.kill()
    finally:
        stream.close()  # type: ignore[attr-defined]
    result[key] = digest.hexdigest()
    result[f"{key}_exceeded"] = exceeded


def _run_held_out(command_id: str, repository: Path) -> CodingBehaviorCommandEvidence:
    arguments = _HELD_OUT_COMMANDS.get(command_id)
    if arguments is None:
        raise ValueError("held-out command is not in the trusted catalog")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
    }
    try:
        process = subprocess.Popen(
            (sys.executable, *arguments),
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        empty = sha256(b"").hexdigest()
        return CodingBehaviorCommandEvidence(
            command_id=command_id,
            returncode=None,
            stdout_digest=empty,
            stderr_digest=empty,
            error_category="spawn_failed",
        )
    assert process.stdout is not None and process.stderr is not None
    digests: dict[str, object] = {}
    threads = (
        threading.Thread(target=_digest_pipe, args=(process.stdout, digests, "stdout", process), daemon=True),
        threading.Thread(target=_digest_pipe, args=(process.stderr, digests, "stderr", process), daemon=True),
    )
    for thread in threads:
        thread.start()
    category: Literal["none", "failed", "timed_out", "output_limit", "spawn_failed"]
    try:
        returncode = process.wait(timeout=_COMMAND_TIMEOUT_SECONDS)
        category = "none" if returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait(timeout=2)
        category = "timed_out"
    for thread in threads:
        thread.join(timeout=2)
    if digests.get("stdout_exceeded") or digests.get("stderr_exceeded"):
        category = "output_limit"
    empty = sha256(b"").hexdigest()
    return CodingBehaviorCommandEvidence(
        command_id=command_id,
        returncode=returncode,
        stdout_digest=str(digests.get("stdout", empty)),
        stderr_digest=str(digests.get("stderr", empty)),
        error_category=category,
    )


def grade_coding_behavior_case(
    case: CodingBehaviorCase,
    value: CodingBehaviorGradeInput,
) -> CodingBehaviorGradingReport:
    """Run the case's fixed grader inventory without trusting model-reported results."""

    if case.fixture_id != value.fixture.fixture_id:
        raise ValueError("case fixture binding mismatch")
    try:
        changed_paths = _actual_changed_paths(value)
        final_tree = _git(
            value.fixture.repository, "rev-parse", f"{value.final_commit}^{{tree}}"
        ).decode().strip()
        main_commit = _git(
            value.fixture.repository, "rev-parse", "refs/heads/main"
        ).decode().strip()
        worktree_clean = not _git(
            value.fixture.repository,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        forbidden_unchanged = all(
            sha256(
                _git(
                    value.fixture.repository,
                    "ls-tree",
                    value.final_commit,
                    "--",
                    path,
                )
            ).hexdigest()
            == digest
            for path, digest in value.fixture.forbidden_entry_digests
        )
        command_evidence = _run_held_out(
            value.fixture.held_out_command_id, value.fixture.repository
        )
        actual = set(changed_paths)
        scope_passed = (
            set(case.expected_changed_paths).issubset(actual)
            and actual.issubset(case.allowed_changed_paths)
        )
        lifecycle_passed = value.interrupt_kinds == case.required_interrupts
        integration_passed = (
            value.final_commit == main_commit
            and worktree_clean
            and final_tree
            == value.validation_tree_digest
            == value.review_tree_digest
            == value.integration_tree_digest
        )
        bounded_passed = (
            value.elapsed_ms <= case.max_runtime_seconds * 1000
            and value.interrupt_count == len(value.interrupt_kinds)
            and value.interrupt_count <= value.max_interrupts
            and value.evidence_size_bytes <= value.max_evidence_size_bytes
        )
        outcomes = {
            "terminal_status": (value.terminal_status == "merged", "Coding run did not reach the merged terminal."),
            "held_out_tests": (command_evidence.error_category == "none", "Held-out command failed."),
            "changed_path_scope": (scope_passed, "Changed paths violated the case scope."),
            "forbidden_paths_unchanged": (forbidden_unchanged, "A forbidden path changed."),
            "native_lifecycle": (lifecycle_passed, "Native interrupt lifecycle did not match the case."),
            "integration_binding": (integration_passed, "Integrated tree did not match validation and review."),
            "bounded_execution": (bounded_passed, "Execution exceeded a declared bound."),
        }
        checks = tuple(
            _check(check_id, *outcomes[check_id]) for check_id in case.grader_ids
        )
    except Exception:
        changed_paths = ()
        command_evidence = None
        checks = tuple(
            _check(check_id, False, "Deterministic grader raised an internal error.")
            for check_id in case.grader_ids
        )
    return CodingBehaviorGradingReport(
        status="passed" if all(check.status == "passed" for check in checks) else "failed",
        checks=checks,
        changed_paths=changed_paths,
        command_evidence=command_evidence,
    )


if tuple(sorted(TRUSTED_GRADER_IDS)) != tuple(
    sorted(
        {
            "bounded_execution",
            "changed_path_scope",
            "forbidden_paths_unchanged",
            "held_out_tests",
            "integration_binding",
            "native_lifecycle",
            "terminal_status",
        }
    )
):
    raise RuntimeError("trusted grader implementation catalog does not match the contract")


__all__ = [
    "CodingBehaviorCommandEvidence",
    "CodingBehaviorGradeInput",
    "CodingBehaviorGradingReport",
    "grade_coding_behavior_case",
]

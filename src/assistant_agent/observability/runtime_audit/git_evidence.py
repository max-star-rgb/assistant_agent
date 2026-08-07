"""Bounded, read-only Git evidence for daily runtime audits."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
from typing import Any

from assistant_agent.observability.runtime_audit.safety import (
    sanitize_runtime_audit_text,
)


DEFAULT_MAX_COMMITS = 80
DEFAULT_MAX_PATCH_CHARS = 120_000


def collect_repository_change_evidence(
    *,
    repo_root: Path,
    window_start: datetime,
    collected_at: datetime,
    max_commits: int = DEFAULT_MAX_COMMITS,
    max_patch_chars: int = DEFAULT_MAX_PATCH_CHARS,
) -> dict[str, Any]:
    """Collect commits made between the audit window start and collection time."""

    if window_start.tzinfo is None or window_start.utcoffset() is None:
        raise ValueError("window_start must be timezone-aware")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    if collected_at < window_start:
        raise ValueError("collected_at must not precede window_start")
    if max_commits <= 0 or max_patch_chars < 0:
        raise ValueError("Git evidence budgets are invalid")

    repo_root = Path(repo_root).resolve()
    try:
        head = _git(repo_root, "rev-parse", "HEAD").strip()
        all_shas = [
            value
            for value in _git(
                repo_root,
                "rev-list",
                "--reverse",
                f"--since={window_start.isoformat()}",
                f"--until={collected_at.isoformat()}",
                "HEAD",
            ).splitlines()
            if value
        ]
        omitted_commit_count = max(0, len(all_shas) - max_commits)
        shas = all_shas[-max_commits:]
        per_commit_patch_chars = (
            min(4_000, max_patch_chars // len(shas)) if shas else 0
        )
        commits = [
            _commit_evidence(
                repo_root,
                sha,
                max_patch_chars=per_commit_patch_chars,
            )
            for sha in shas
        ]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {
            "available": False,
            "window_start": window_start.isoformat(),
            "collected_at": collected_at.isoformat(),
            "error": sanitize_runtime_audit_text(exc),
            "commits": [],
        }
    return {
        "available": True,
        "repository_head": head,
        "window_start": window_start.isoformat(),
        "collected_at": collected_at.isoformat(),
        "commit_count": len(commits),
        "omitted_commit_count": omitted_commit_count,
        "commits": commits,
    }


def _commit_evidence(
    repo_root: Path,
    sha: str,
    *,
    max_patch_chars: int,
) -> dict[str, Any]:
    header = _git(repo_root, "show", "-s", "--format=%H%x00%cI%x00%s", sha)
    full_sha, committed_at, subject = header.rstrip("\n").split("\x00", maxsplit=2)
    files = sorted(
        value
        for value in _git(
            repo_root,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            sha,
        ).splitlines()
        if value
    )
    implementation_files = [
        value
        for value in files
        if value.startswith(("src/assistant_agent/", "tests/"))
    ]
    patch_files = implementation_files or files
    patch = _git(
        repo_root,
        "show",
        "--format=",
        "--no-ext-diff",
        "--no-renames",
        "--unified=1",
        sha,
        "--",
        *patch_files,
    )
    return {
        "sha": full_sha,
        "committed_at": committed_at,
        "subject": subject,
        "files": files,
        "patch_files": patch_files,
        "patch_excerpt": patch[:max_patch_chars],
        "patch_truncated": len(patch) > max_patch_chars,
    }


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        detail = sanitize_runtime_audit_text(result.stderr[-1_000:] or "git failed")
        raise ValueError(detail)
    return result.stdout

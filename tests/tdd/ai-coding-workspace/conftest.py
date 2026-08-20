from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from assistant_agent.coding.config import CodingConfig, CodingRepositoryConfig


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "coding-test@example.invalid")
    run_git(repo, "config", "user.name", "Coding Test")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("greeting = 'hello'\n", encoding="utf-8")
    (repo / "README.md").write_text("# Fixture\nhello repository\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "fixture")
    return repo


@pytest.fixture
def coding_config(tmp_path: Path, source_repo: Path) -> CodingConfig:
    repository = CodingRepositoryConfig(
        repo_id="repo",
        path=source_repo.resolve(),
        target_branch="main",
    )
    return CodingConfig(
        enabled=True,
        workspace_root=(tmp_path / "workspaces").resolve(),
        repositories={"repo": repository},
        ttl_seconds=300,
        max_patch_bytes=32_768,
        max_changed_files=8,
        max_file_bytes=16_384,
    )


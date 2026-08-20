from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from assistant_agent.coding.config import CodingConfig, CodingRepositoryConfig
from assistant_agent.coding.workspace import CodingWorkspaceService


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def workspace_bundle(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.email", "coding-test@example.invalid")
    git(source, "config", "user.name", "Coding Test")
    (source / "a.py").write_text("value = 'a'\n", encoding="utf-8")
    (source / "b.py").write_text("value = 'b'\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "fixture")
    config = CodingConfig(
        enabled=True,
        workspace_root=(tmp_path / "workspaces").resolve(),
        repositories={
            "repo": CodingRepositoryConfig(
                repo_id="repo",
                path=source.resolve(),
                target_branch="main",
            )
        },
        ttl_seconds=300,
        max_patch_bytes=32_768,
        max_changed_files=8,
        max_file_bytes=16_384,
    )
    service = CodingWorkspaceService(config, secret=b"test-secret")
    workspace = service.resolve("user-a", "thread-a", "repo")
    return service, workspace


"""Temporary RED/GREEN coverage for async worker repository snapshots."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from assistant_agent.agent_server import async_delegation
from assistant_agent.agent_server.async_delegation import (
    BACKGROUND_AGENT_NAME,
    build_async_subagent_middleware,
)
from assistant_agent.coding.config import CodingConfig, CodingRepositoryConfig
from assistant_agent.coding.workspace import CodingWorkspaceError, CodingWorkspaceService
from assistant_agent.native_agent.context import (
    ASSISTANT_RUNTIME_METADATA_KEY,
    AssistantRuntimeFacts,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    return repo


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _coding_config(tmp_path: Path, repo: Path) -> CodingConfig:
    return CodingConfig(
        enabled=True,
        workspace_root=tmp_path / "workspaces",
        repositories={
            "repo-sentinel": CodingRepositoryConfig(
                repo_id="repo-sentinel",
                path=repo,
                target_branch="main",
            )
        },
    )


def test_explicit_base_commit_survives_source_head_movement(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    first = _commit(repo, "first.txt", "first")
    service = CodingWorkspaceService(_coding_config(tmp_path, repo))
    second = _commit(repo, "second.txt", "second")

    workspace = service.resolve(
        "user-sentinel",
        "worker-thread-sentinel",
        "repo-sentinel",
        base_commit=first,
    )

    assert second != first
    assert service.git_head(workspace.root) == first
    assert workspace.base_commit == first


def test_existing_workspace_rejects_a_different_base_commit(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    first = _commit(repo, "first.txt", "first")
    service = CodingWorkspaceService(_coding_config(tmp_path, repo))
    service.resolve(
        "user-sentinel", "thread-sentinel", "repo-sentinel", base_commit=first
    )
    second = _commit(repo, "second.txt", "second")

    with pytest.raises(CodingWorkspaceError) as exc_info:
        service.resolve(
            "user-sentinel", "thread-sentinel", "repo-sentinel", base_commit=second
        )

    assert exc_info.value.code == "workspace_base_commit_mismatch"


def test_explicit_base_commit_must_exist_in_repository(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    _commit(repo, "first.txt", "first")
    service = CodingWorkspaceService(_coding_config(tmp_path, repo))

    with pytest.raises(CodingWorkspaceError) as exc_info:
        service.resolve(
            "user-sentinel",
            "thread-sentinel",
            "repo-sentinel",
            base_commit="f" * 40,
        )

    assert exc_info.value.code == "workspace_base_commit_invalid"


def test_async_worker_requires_repository_snapshot_sha() -> None:
    with pytest.raises(ValidationError, match="repository snapshot sha"):
        AssistantRuntimeFacts(entry_profile="async_worker")

    assert AssistantRuntimeFacts(entry_profile="agent_server").repository_snapshot_sha is None


def _snapshot_sha(metadata: Mapping[str, Any]) -> str:
    return metadata[ASSISTANT_RUNTIME_METADATA_KEY]["repository_snapshot_sha"]


def _async_client() -> SimpleNamespace:
    return SimpleNamespace(
        threads=SimpleNamespace(
            create=AsyncMock(return_value={"thread_id": "worker-thread"}),
        ),
        runs=SimpleNamespace(create=AsyncMock(return_value={"run_id": "worker-run"})),
        aclose=AsyncMock(),
    )


def _runtime(*, async_tasks: dict[str, dict[str, Any]] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state={"memory_context": ("memory-sentinel",), "async_tasks": async_tasks or {}},
        config={"configurable": {"thread_id": "parent-thread"}, "run_id": "parent-run"},
        tool_call_id="tool-call",
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-sentinel")),
    )


def _tool(middleware: Any, name: str) -> Any:
    return next(tool for tool in middleware.tools if tool.name == name)


def test_start_keeps_creation_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(repository_head=lambda repo_id: "a" * 40)
    client = _async_client()
    monkeypatch.setattr(async_delegation, "get_client", lambda **kwargs: client)
    middleware = build_async_subagent_middleware(service, "repo-sentinel")
    start = _tool(middleware, "start_async_task")
    command = asyncio.run(
        start.coroutine(
            description="background work",
            subagent_type=BACKGROUND_AGENT_NAME,
            runtime=_runtime(),
        )
    )
    task = next(iter(command.update["async_tasks"].values()))

    assert task["repository_snapshot_sha"] == "a" * 40
    assert _snapshot_sha(client.threads.create.await_args.kwargs["metadata"]) == "a" * 40
    assert _snapshot_sha(client.runs.create.await_args.kwargs["metadata"]) == "a" * 40
    assert client.runs.create.await_args.kwargs["input"] == {
        "messages": [{"role": "user", "content": "background work"}],
        "memory_context": ["memory-sentinel"],
    }


def test_update_reuses_handle_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _async_client()
    monkeypatch.setattr(async_delegation, "get_client", lambda **kwargs: client)
    task = {
        "task_id": "task-sentinel",
        "agent_name": BACKGROUND_AGENT_NAME,
        "thread_id": "worker-thread",
        "run_id": "worker-run",
        "parent_thread_id": "parent-thread",
        "parent_run_id": "parent-run",
        "repository_snapshot_sha": "a" * 40,
        "status": "running",
        "created_at": "2026-08-27T00:00:00Z",
        "last_checked_at": "2026-08-27T00:00:00Z",
        "last_updated_at": "2026-08-27T00:00:00Z",
    }

    result = asyncio.run(
        async_delegation._update_async_task(
            "task-sentinel", "follow up", _runtime(async_tasks={"task-sentinel": task})
        )
    )

    assert not isinstance(result, str)
    assert _snapshot_sha(client.runs.create.await_args.kwargs["metadata"]) == "a" * 40
    assert client.runs.create.await_args.kwargs["input"] == {
        "messages": [{"role": "user", "content": "follow up"}]
    }


@pytest.mark.parametrize("repository_snapshot_sha", [None, "invalid"])
def test_update_rejects_missing_or_invalid_handle_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    repository_snapshot_sha: str | None,
) -> None:
    client = _async_client()
    monkeypatch.setattr(async_delegation, "get_client", lambda **kwargs: client)
    task = {
        "task_id": "task-sentinel",
        "agent_name": BACKGROUND_AGENT_NAME,
        "thread_id": "worker-thread",
        "run_id": "worker-run",
        "parent_thread_id": "parent-thread",
        "parent_run_id": "parent-run",
        "repository_snapshot_sha": repository_snapshot_sha,
        "status": "running",
        "created_at": "2026-08-27T00:00:00Z",
        "last_checked_at": "2026-08-27T00:00:00Z",
        "last_updated_at": "2026-08-27T00:00:00Z",
    }

    result = asyncio.run(
        async_delegation._update_async_task(
            "task-sentinel", "follow up", _runtime(async_tasks={"task-sentinel": task})
        )
    )

    assert isinstance(result, str)
    assert result.startswith("Failed to update async subagent snapshot:")
    assert client.runs.create.await_count == 0

"""Deep Agents backends bound to authenticated coding worktrees."""

from __future__ import annotations

import os
from collections.abc import Mapping

from deepagents.backends import FilesystemBackend, LocalShellBackend
from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from langgraph.config import get_config
from langgraph.runtime import get_runtime
from pydantic import ValidationError

from assistant_agent.coding.workspace import (
    CodingWorkspace,
    CodingWorkspaceError,
    CodingWorkspaceService,
)
from assistant_agent.native_agent.context import (
    ASSISTANT_RUNTIME_METADATA_KEY,
    AssistantRunContext,
    AssistantRuntimeFacts,
    assistant_runtime_facts,
    authenticated_user_identity,
)


def _workspace(
    service: CodingWorkspaceService,
    repo_id: str,
) -> CodingWorkspace:
    runtime = get_runtime(AssistantRunContext)
    config = get_config()
    thread_id = str(config.get("configurable", {}).get("thread_id", ""))
    metadata = config.get("metadata")
    raw_facts = (
        metadata.get(ASSISTANT_RUNTIME_METADATA_KEY)
        if isinstance(metadata, Mapping)
        else None
    )
    if isinstance(raw_facts, Mapping) and raw_facts.get("entry_profile") == "async_worker":
        if not raw_facts.get("repository_snapshot_sha"):
            raise CodingWorkspaceError("workspace_snapshot_required")
        try:
            facts = AssistantRuntimeFacts.model_validate(dict(raw_facts))
        except ValidationError as exc:
            raise CodingWorkspaceError("workspace_snapshot_invalid") from exc
    else:
        facts = assistant_runtime_facts(config)
    return service.resolve(
        authenticated_user_identity(runtime),
        thread_id,
        repo_id,
        base_commit=facts.repository_snapshot_sha,
    )


class CodingWorkspaceBackend(SandboxBackendProtocol):
    """Resolve the current authenticated thread worktree, then use Deep Agents I/O."""

    def __init__(self, service: CodingWorkspaceService, repo_id: str) -> None:
        self._service = service
        self._repo_id = repo_id

    @property
    def id(self) -> str:
        return "assistant-coding-workspace"

    def _backend(self) -> LocalShellBackend:
        workspace = _workspace(self._service, self._repo_id)
        return LocalShellBackend(
            root_dir=workspace.root,
            virtual_mode=True,
            timeout=120,
            max_output_bytes=100_000,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            inherit_env=False,
        )

    def ls(self, path: str):
        return self._backend().ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self._backend().read(file_path, offset, limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ):
        return self._backend().grep(
            pattern,
            path=path,
            glob=glob,
            max_count=max_count,
        )

    def glob(self, pattern: str, path: str | None = None):
        return self._backend().glob(pattern, path)

    def write(self, file_path: str, content: str):
        return self._backend().write(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        return self._backend().edit(
            file_path,
            old_string,
            new_string,
            replace_all=replace_all,
        )

    def delete(self, file_path: str):
        return self._backend().delete(file_path)

    def execute(self, command: str, *, timeout: int | None = None):
        return self._backend().execute(command, timeout=timeout)


class ReadOnlyCodingWorkspaceBackend(BackendProtocol):
    """Resolve the current worktree and expose only read operations."""

    def __init__(self, service: CodingWorkspaceService, repo_id: str) -> None:
        self._service = service
        self._repo_id = repo_id

    def _backend(self) -> FilesystemBackend:
        workspace = _workspace(self._service, self._repo_id)
        return FilesystemBackend(root_dir=workspace.root, virtual_mode=True)

    def ls(self, path: str):
        return self._backend().ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self._backend().read(file_path, offset, limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ):
        return self._backend().grep(
            pattern,
            path=path,
            glob=glob,
            max_count=max_count,
        )

    def glob(self, pattern: str, path: str | None = None):
        return self._backend().glob(pattern, path)


__all__ = ["CodingWorkspaceBackend", "ReadOnlyCodingWorkspaceBackend"]

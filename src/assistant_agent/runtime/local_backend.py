"""Deep Agents backends for local OS access and thread resource shortcuts."""

from __future__ import annotations

import os
from pathlib import Path

from deepagents.backends import CompositeBackend, LocalShellBackend
from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from langgraph.config import get_config
from langgraph.runtime import get_runtime

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.runtime.thread_resources import ThreadResourceManager


class HomeShellBackend(LocalShellBackend):
    def __init__(self, *, agent_home: Path) -> None:
        super().__init__(
            root_dir=agent_home,
            virtual_mode=False,
            timeout=120,
            max_output_bytes=100_000,
            env=_shell_env(),
            inherit_env=False,
        )

    def _resolve_path(self, key: str) -> Path:
        return super()._resolve_path("." if key.rstrip("/") in {".", "/."} else key)


class ReadOnlyHomeBackend(BackendProtocol):
    def __init__(self, *, agent_home: Path) -> None:
        self._backend = HomeShellBackend(agent_home=agent_home)

    def ls(self, path: str):
        return self._backend.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self._backend.read(file_path, offset, limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ):
        return self._backend.grep(
            pattern,
            path=path,
            glob=glob,
            max_count=max_count,
        )

    def glob(self, pattern: str, path: str | None = None):
        return self._backend.glob(pattern, path)


class ReadOnlyThreadDirectoryBackend(BackendProtocol):
    def __init__(self, manager: ThreadResourceManager, attribute: str) -> None:
        self._backend = ThreadDirectoryBackend(manager, attribute)

    def ls(self, path: str):
        return self._backend.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self._backend.read(file_path, offset, limit)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None):
        return self._backend.grep(pattern, path=path, glob=glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None):
        return self._backend.glob(pattern, path)


class ThreadDirectoryBackend(SandboxBackendProtocol):
    def __init__(self, manager: ThreadResourceManager, attribute: str) -> None:
        self._manager = manager
        self._attribute = attribute

    @property
    def id(self) -> str:
        return f"thread-{self._attribute}"

    def _resource_backend(self) -> LocalShellBackend:
        runtime = get_runtime(AssistantRunContext)
        thread_id = str(
            get_config().get("configurable", {}).get("thread_id", "")
        ).strip()
        resources = self._manager.resolve(
            authenticated_user_identity(runtime),
            thread_id,
        )
        return LocalShellBackend(
            root_dir=getattr(resources, self._attribute),
            virtual_mode=True,
            timeout=120,
            max_output_bytes=100_000,
            env=_shell_env(),
            inherit_env=False,
        )

    def ls(self, path: str):
        return self._resource_backend().ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self._resource_backend().read(file_path, offset, limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ):
        return self._resource_backend().grep(
            pattern,
            path=path,
            glob=glob,
            max_count=max_count,
        )

    def glob(self, pattern: str, path: str | None = None):
        return self._resource_backend().glob(pattern, path)

    def write(self, file_path: str, content: str):
        return self._resource_backend().write(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        return self._resource_backend().edit(
            file_path,
            old_string,
            new_string,
            replace_all=replace_all,
        )

    def delete(self, file_path: str):
        return self._resource_backend().delete(file_path)


def create_local_backend(
    manager: ThreadResourceManager,
    *,
    agent_home: Path = Path.home() / "assistant_agent",
) -> CompositeBackend:
    return CompositeBackend(
        default=HomeShellBackend(agent_home=agent_home),
        routes={
            "/artifacts/": ThreadDirectoryBackend(manager, "artifact_root"),
            "/scratch/": ThreadDirectoryBackend(manager, "scratch_root"),
            "/uploads/": ThreadDirectoryBackend(manager, "upload_root"),
        },
        artifacts_root="/artifacts/",
    )


def create_browser_backend(manager: ThreadResourceManager) -> CompositeBackend:
    scratch = ReadOnlyThreadDirectoryBackend(manager, "scratch_root")
    return CompositeBackend(
        default=scratch,
        routes={
            "/artifacts/": ReadOnlyThreadDirectoryBackend(manager, "artifact_root"),
            "/scratch/": scratch,
        },
        artifacts_root="/artifacts/",
    )


def _shell_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


__all__ = [
    "HomeShellBackend",
    "ReadOnlyHomeBackend",
    "ReadOnlyThreadDirectoryBackend",
    "ThreadDirectoryBackend",
    "create_browser_backend",
    "create_local_backend",
]

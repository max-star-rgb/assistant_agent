"""Deep Agents backends for local OS access."""

from __future__ import annotations

import os
from pathlib import Path

from deepagents.backends import FilesystemBackend, LocalShellBackend
from langgraph.runtime import get_runtime

from assistant_agent.native_agent.context import AssistantRunContext


class WorkingDirectoryBackend(LocalShellBackend):
    """Resolve the native local backend from the current run context."""

    def __init__(
        self,
        subdirectory: str | None = None,
        *,
        virtual_mode: bool = False,
    ) -> None:
        self.cwd = Path.home()
        self.virtual_mode = virtual_mode
        self._subdirectory = subdirectory
        self.max_file_size_bytes = 10 * 1024 * 1024
        self._default_timeout = 120
        self._max_output_bytes = 100_000
        self._env = _shell_env()

    @property
    def id(self) -> str:
        return "local-working-directory"

    def _backend(self) -> LocalShellBackend:
        runtime = get_runtime(AssistantRunContext)
        root_dir = Path(runtime.context.cwd)
        if self._subdirectory:
            root_dir /= self._subdirectory
        return LocalShellBackend(
            root_dir=root_dir,
            virtual_mode=self.virtual_mode,
            timeout=120,
            max_output_bytes=100_000,
            env=_shell_env(),
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

    def upload_files(self, files: list[tuple[str, bytes]]):
        return self._backend().upload_files(files)

    async def aupload_files(self, files: list[tuple[str, bytes]]):
        return await self._backend().aupload_files(files)

    def download_files(self, paths: list[str]):
        return self._backend().download_files(paths)

    async def adownload_files(self, paths: list[str]):
        return await self._backend().adownload_files(paths)

    def execute(self, command: str, *, timeout: int | None = None):
        return self._backend().execute(command, timeout=timeout)


class UserHomeBackend(FilesystemBackend):
    """Filesystem backend rooted at the current OS user's home."""

    def __init__(self, subdirectory: str | None = None) -> None:
        self.cwd = Path.home() / subdirectory if subdirectory else Path.home()
        self.virtual_mode = True
        self.max_file_size_bytes = 10 * 1024 * 1024


def create_local_backend() -> WorkingDirectoryBackend:
    return WorkingDirectoryBackend()


def create_conversation_history_backend() -> UserHomeBackend:
    """Store Deep Agents history under the current OS user's home."""

    return UserHomeBackend()


def _shell_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


__all__ = [
    "UserHomeBackend",
    "WorkingDirectoryBackend",
    "create_conversation_history_backend",
    "create_local_backend",
]

"""Deep Agents backends for local OS access."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from deepagents.backends import FilesystemBackend, LocalShellBackend
from langgraph.runtime import get_runtime
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from assistant_agent.native_agent.context import AssistantRunContext


class DeviceConnectionMiddleware(AgentMiddleware):
    """A bound device must remain connected even for same-host business tools."""

    @staticmethod
    def _blocked(request):
        if request.runtime.context.device_id is None:
            return None
        from assistant_agent.agent_server.devices import local_devices
        from assistant_agent.native_agent.context import authenticated_user_identity
        try:
            local_devices.require_binding(
                authenticated_user_identity(request.runtime), request.runtime.context, connected=True,
            )
        except ValueError as exc:
            return ToolMessage(content=str(exc), name=request.tool_call["name"],
                               tool_call_id=request.tool_call["id"], status="error")
        return None

    def wrap_tool_call(self, request, handler):
        blocked = self._blocked(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        blocked = self._blocked(request)
        return blocked if blocked is not None else await handler(request)


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
        if runtime.context.device_id is not None:
            raise RuntimeError("device execution requires the asynchronous backend API")
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

    async def _async_backend(self):
        runtime = get_runtime(AssistantRunContext)
        if runtime.context.device_id is not None:
            from assistant_agent.agent_server.devices import local_devices
            from assistant_agent.native_agent.context import authenticated_user_identity
            from assistant_agent.runtime.device_backend import DeviceBackend
            owner = authenticated_user_identity(runtime)
            local_devices.require_binding(owner, runtime.context)
            return DeviceBackend(local_devices.broker, runtime.context.device_id, owner=owner, subdirectory=self._subdirectory)
        return await asyncio.to_thread(self._backend)

    async def als(self, path):
        return await (await self._async_backend()).als(path)

    async def aread(self, file_path, offset=0, limit=2000):
        return await (await self._async_backend()).aread(file_path, offset, limit)

    async def awrite(self, file_path, content):
        return await (await self._async_backend()).awrite(file_path, content)

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        return await (await self._async_backend()).aedit(file_path, old_string, new_string, replace_all)

    async def adelete(self, file_path):
        return await (await self._async_backend()).adelete(file_path)

    async def aglob(self, pattern, path=None):
        return await (await self._async_backend()).aglob(pattern, path)

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        return await (await self._async_backend()).agrep(pattern, path, glob, max_count=max_count)

    async def aexecute(self, command, *, timeout=None):
        return await (await self._async_backend()).aexecute(command, timeout=timeout)

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
        return await (await self._async_backend()).aupload_files(files)

    def download_files(self, paths: list[str]):
        return self._backend().download_files(paths)

    async def adownload_files(self, paths: list[str]):
        return await (await self._async_backend()).adownload_files(paths)

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


def create_conversation_history_backend() -> WorkingDirectoryBackend:
    """Store generated compaction history in the selected execution workspace."""

    return WorkingDirectoryBackend(".agent/history", virtual_mode=True)


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

"""Official Deep Agents coding harness bound to isolated repository worktrees."""

from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain.agents.middleware import TodoListMiddleware
from langgraph.config import get_config
from langgraph.runtime import get_runtime

from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.native_agent.fast_agent import RecursionFinalSynthesisMiddleware
from assistant_agent.native_agent.tool_call_limits import PerToolCallLimitMiddleware


_CODING_PROMPT = """你是仓库编码 Agent。先读取仓库和 AGENTS.md，再直接完成用户要求并运行最小相关验证。
只操作当前 workspace；不要提交、合并或推送 Git。写文件、删除文件和执行命令会由运行时请求用户审批。"""


class CodingWorkspaceBackend(SandboxBackendProtocol):
    """Resolve the current authenticated thread worktree, then use Deep Agents I/O."""

    def __init__(self, service: CodingWorkspaceService, repo_id: str) -> None:
        self._service = service
        self._repo_id = repo_id

    @property
    def id(self) -> str:
        return "assistant-coding-workspace"

    def _backend(self) -> LocalShellBackend:
        runtime = get_runtime(AssistantRunContext)
        thread_id = str(get_config().get("configurable", {}).get("thread_id", ""))
        workspace = self._service.resolve(
            authenticated_user_identity(runtime),
            thread_id,
            self._repo_id,
        )
        return LocalShellBackend(
            root_dir=workspace.root,
            virtual_mode=True,
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


def build_coding_agent(
    model: Any,
    workspace_service: CodingWorkspaceService,
    *,
    repo_id: str,
):
    """Build one process-owned Deep Agent without a private checkpointer."""

    return create_deep_agent(
        model=model,
        system_prompt=_CODING_PROMPT,
        middleware=[
            TodoListMiddleware(),
            PerToolCallLimitMiddleware(max_parallel_calls_per_tool=12),
            RecursionFinalSynthesisMiddleware(),
        ],
        backend=CodingWorkspaceBackend(workspace_service, repo_id),
        interrupt_on={
            "write_file": True,
            "edit_file": True,
            "delete": True,
            "execute": True,
        },
        context_schema=AssistantRunContext,
        name="AssistantCodingAgent",
    )


__all__ = ["CodingWorkspaceBackend", "build_coding_agent"]

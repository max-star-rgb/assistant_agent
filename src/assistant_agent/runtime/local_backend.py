"""Deep Agents backends for local OS access."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from deepagents.backends import FilesystemBackend, LocalShellBackend
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.runtime import get_runtime
from langgraph.types import Command
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.native_boundary import configure_builtin_tool


GIT_TOOL_NAME = "git"
_EXECUTE_GIT_RULE = "Git 操作优先使用 git Tool；execute 不执行 Git 命令。"
_EXECUTE_GIT_REDIRECT = {
    "status": "failed",
    "error": "use_git_tool",
    "summary": "execute 不执行 Git 命令；请先激活 git Tool Profile，再使用 git Tool。",
}


@dataclass(frozen=True)
class GitCommandResult:
    output: str
    exit_code: int
    repository_root: Path | None


def is_git_shell_command(command: str) -> bool:
    """Return whether a shell command directly invokes the Git CLI."""

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    expects_command = True
    for token in tokens:
        if token in {";", "&&", "||", "|", "&", "("}:
            expects_command = True
            continue
        if not expects_command:
            continue
        if token in {"command", "env", "nohup", "sudo"} or "=" in token:
            continue
        if Path(token).name == "git":
            return True
        expects_command = False
    return False


def run_git_command(
    target_path: str | Path,
    arguments: tuple[str, ...],
    *,
    timeout: int = 120,
) -> GitCommandResult:
    """Resolve the containing repository and run one argument-safe Git command."""

    target = Path(target_path).expanduser().resolve()
    directory = target.parent if target.is_file() else target
    resolved = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if resolved.returncode != 0:
        return GitCommandResult(
            output=(resolved.stderr or resolved.stdout).strip(),
            exit_code=resolved.returncode,
            repository_root=None,
        )
    repository_root = Path(resolved.stdout.strip()).resolve()
    if _overrides_git_repository(arguments):
        return GitCommandResult(
            output="Git arguments cannot override the target repository.",
            exit_code=2,
            repository_root=repository_root,
        )
    completed = subprocess.run(
        ["git", "-C", str(repository_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return GitCommandResult(
        output=(completed.stdout or completed.stderr).strip(),
        exit_code=completed.returncode,
        repository_root=repository_root,
    )


def _overrides_git_repository(arguments: tuple[str, ...]) -> bool:
    skip_value = False
    for argument in arguments:
        if skip_value:
            skip_value = False
            continue
        if argument in {"-C", "--bare", "--git-dir", "--work-tree"}:
            return True
        if argument.startswith(("--git-dir=", "--work-tree=")):
            return True
        if argument in {"-c", "--config-env"}:
            skip_value = True
            continue
        if not argument.startswith("-"):
            break
    return False


class GitToolMiddleware(AgentMiddleware):
    """Expose repository-aware Git and keep Git out of the generic shell Tool."""

    def __init__(self) -> None:
        super().__init__()
        self.tools = [self._create_git_tool()]

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse | AIMessage],
    ) -> ModelResponse | AIMessage:
        return handler(self._with_execute_rule(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse | AIMessage]],
    ) -> ModelResponse | AIMessage:
        return await handler(self._with_execute_rule(request))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        blocked = self._blocked_execute(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        blocked = self._blocked_execute(request)
        return blocked if blocked is not None else await handler(request)

    @staticmethod
    def _with_execute_rule(request: ModelRequest) -> ModelRequest:
        tools: list[BaseTool | dict[str, Any]] = []
        for candidate in request.tools:
            name = (
                candidate.name
                if isinstance(candidate, BaseTool)
                else candidate.get("name")
            )
            description = (
                candidate.description
                if isinstance(candidate, BaseTool)
                else candidate.get("description", "")
            )
            if name != "execute" or _EXECUTE_GIT_RULE in description:
                tools.append(candidate)
            elif isinstance(candidate, BaseTool):
                tools.append(
                    candidate.model_copy(
                        update={"description": f"{description}\n- {_EXECUTE_GIT_RULE}"}
                    )
                )
            else:
                tools.append(
                    {
                        **candidate,
                        "description": f"{description}\n- {_EXECUTE_GIT_RULE}",
                    }
                )
        return request.override(tools=tools)

    @staticmethod
    def _blocked_execute(request: ToolCallRequest) -> ToolMessage | None:
        if request.tool_call["name"] != "execute":
            return None
        command = request.tool_call.get("args", {}).get("command")
        if not isinstance(command, str) or not is_git_shell_command(command):
            return None
        return ToolMessage(
            content=json.dumps(
                _EXECUTE_GIT_REDIRECT, ensure_ascii=False, sort_keys=True
            ),
            artifact=dict(_EXECUTE_GIT_REDIRECT),
            name="execute",
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    @staticmethod
    def _create_git_tool() -> BaseTool:
        @tool(GIT_TOOL_NAME, response_format="content_and_artifact")
        def git_command(
            target_path: Annotated[
                str,
                Field(
                    min_length=1,
                    max_length=4_096,
                    description="仓库内目标文件或目录路径；相对路径基于当前工作目录。",
                ),
            ],
            arguments: Annotated[
                list[str],
                Field(
                    min_length=1,
                    max_length=128,
                    description="Git 参数数组，不包含开头的 git。",
                ),
            ],
            runtime: ToolRuntime[AssistantRunContext],
        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            """在目标路径所属仓库根目录执行 Git，不依赖当前 shell 目录。"""

            if Path(arguments[0]).name == GIT_TOOL_NAME:
                raise ToolException("arguments 不应包含开头的 git。")
            target = Path(target_path).expanduser()
            if not target.is_absolute():
                target = runtime.context.cwd / target
            result = run_git_command(target, tuple(arguments))
            observation = {
                "status": "succeeded" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "output": result.output,
                "repository_root": (
                    str(result.repository_root) if result.repository_root else None
                ),
            }
            if result.exit_code != 0:
                raise ToolException(json.dumps(observation, ensure_ascii=False))
            return ([{"type": "text", "text": result.output}], observation)

        return configure_builtin_tool(git_command, bounded_expected_errors=True)


class WorkingDirectoryBackend(LocalShellBackend):
    """Resolve the native local backend from the current run context."""

    def __init__(self) -> None:
        self.cwd = Path.home()
        self.virtual_mode = False
        self.max_file_size_bytes = 10 * 1024 * 1024
        self._default_timeout = 120
        self._max_output_bytes = 100_000
        self._env = _shell_env()

    @property
    def id(self) -> str:
        return "local-working-directory"

    def _backend(self) -> LocalShellBackend:
        runtime = get_runtime(AssistantRunContext)
        return LocalShellBackend(
            root_dir=runtime.context.cwd,
            virtual_mode=False,
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
    "GIT_TOOL_NAME",
    "GitCommandResult",
    "GitToolMiddleware",
    "UserHomeBackend",
    "WorkingDirectoryBackend",
    "create_conversation_history_backend",
    "create_local_backend",
    "is_git_shell_command",
    "run_git_command",
]

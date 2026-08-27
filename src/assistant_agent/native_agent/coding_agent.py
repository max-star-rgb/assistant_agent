"""Official Deep Agents coding harness bound to isolated repository worktrees."""

from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

from assistant_agent.coding.backend import CodingWorkspaceBackend
from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import RecursionFinalSynthesisMiddleware
from assistant_agent.native_agent.tool_call_limits import PerToolCallLimitMiddleware


_CODING_PROMPT = """你是仓库编码 Agent。先读取仓库和 AGENTS.md，再直接完成用户要求并运行最小相关验证。
只操作当前 workspace；不要提交、合并或推送 Git。写文件、删除文件和执行命令会由运行时请求用户审批。"""


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

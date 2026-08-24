"""Planning agent assembled from upstream LangChain and Deep Agents middleware."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from deepagents.backends import StateBackend
from deepagents.middleware import CompiledSubAgent, SubAgentMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import MessageLikeRepresentation

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import (
    MemoryContextMiddleware,
    ToolProgressMiddleware,
    TrustedRuntimeFactsMiddleware,
)
from assistant_agent.native_agent.providers import planning_supervisor_model_view
from assistant_agent.native_agent.state import PlanningAgentState


_WRITE_TODOS_DESCRIPTION_ZH = """创建并管理当前工作会话的结构化待办列表。

只在复杂、多步骤任务中使用。开始执行前把当前事项标记为 in_progress，完成后立即标记为 completed；
如果遇到错误或阻塞，保持 in_progress 并记录需要解决的新事项。简单任务应直接完成，不必创建待办列表。
最后一次更新待办后，还必须另发一条消息交付用户实际要求的结果。"""

_TASK_DESCRIPTION_ZH = """启动一个临时子 Agent，在隔离的上下文窗口中完成复杂、多步骤任务。

可用的子 Agent 类型及能力：
{available_agents}

调用要求：
- 独立任务应在同一条消息中发出多个 task 调用，以便并行执行。
- 每次调用都是无状态的；description 必须包含完整上下文、具体目标和期望输出。
- 子 Agent 的报告不会直接展示给用户；你需要综合结果并自行交付最终答复。
- 明确说明要创建内容、执行操作还是只做分析，不要假设子 Agent 能看到原始用户请求。"""

_GENERAL_PURPOSE_DESCRIPTION_ZH = (
    "通用执行 Agent；使用与主助理相同的业务能力，适合完成复杂、多步骤、上下文密集的任务。"
)


def build_planning_agent(
    model: BaseChatModel,
    fast_agent: Any,
    *,
    model_call_limit: int = 8,
    tool_call_limit: int = 8,
    context_window_tokens: int = 128_000,
    compaction_trigger_ratio: float = 0.75,
    compaction_target_ratio: float = 0.15,
    token_counter: Callable[[Iterable[MessageLikeRepresentation]], int] | None = None,
):
    """Build the native planning coordinator with an executable `task` Tool."""

    if model_call_limit < 1 or tool_call_limit < 1:
        raise ValueError("agent call limits must be positive")
    if context_window_tokens < 100:
        raise ValueError("context window must contain at least 100 tokens")
    if not 0 < compaction_target_ratio < compaction_trigger_ratio <= 1:
        raise ValueError("compaction ratios must satisfy 0 < target < trigger <= 1")

    worker: CompiledSubAgent = {
        "name": "general-purpose",
        "description": _GENERAL_PURPOSE_DESCRIPTION_ZH,
        "runnable": fast_agent,
    }
    summarization_options: dict[str, Any] = {
        "model": model,
        "trigger": (
            "tokens",
            max(1, int(context_window_tokens * compaction_trigger_ratio)),
        ),
        "keep": (
            "tokens",
            max(1, int(context_window_tokens * compaction_target_ratio)),
        ),
        "trim_tokens_to_summarize": None,
    }
    if token_counter is not None:
        summarization_options["token_counter"] = token_counter

    return create_agent(
        model=planning_supervisor_model_view(model),
        tools=[],
        state_schema=PlanningAgentState,
        context_schema=AssistantRunContext,
        middleware=[
            TodoListMiddleware(tool_description=_WRITE_TODOS_DESCRIPTION_ZH),
            SubAgentMiddleware(
                backend=StateBackend(),
                subagents=[worker],
                task_description=_TASK_DESCRIPTION_ZH,
            ),
            ModelCallLimitMiddleware(
                run_limit=model_call_limit,
                exit_behavior="end",
            ),
            ToolCallLimitMiddleware(
                run_limit=tool_call_limit,
                exit_behavior="end",
            ),
            SummarizationMiddleware(**summarization_options),
            MemoryContextMiddleware(),
            TrustedRuntimeFactsMiddleware(),
            ToolProgressMiddleware(),
        ],
        name="AssistantPlanningAgent",
    )


__all__ = ["build_planning_agent"]

"""Reusable fast-mode agent built entirely with LangChain/LangGraph primitives."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    SummarizationMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import MessageLikeRepresentation
from langchain_core.tools import BaseTool

from assistant_agent.native_agent.assistant_agent import (
    MemoryContextMiddleware,
    RecursionFinalSynthesisMiddleware,
    RecursionFinalSynthesisState,
    ToolProgressMiddleware,
    _retryable_read_tool_names,
    _request_with_memory_context,
    _tool_progress_event,
    memory_context_message,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.assistant_prompt import (
    create_assistant_base_prompt,
    create_assistant_runtime_prompt,
)
from assistant_agent.media.visual_perception.history_probe import (
    VisualObservationHistoryProbe,
)
from assistant_agent.native_agent.state import FastAgentState
from assistant_agent.native_agent.conditional_tool_exposure import (
    ConditionalToolExposureMiddleware,
)
from assistant_agent.native_agent.tool_call_limits import (
    PerToolCallLimitMiddleware,
)
from assistant_agent.native_agent.tool_profiles import (
    ToolProfile,
    ToolProfileMiddleware,
    project_tool_profiles,
)
from assistant_agent.skills.native import (
    PROJECT_FILESYSTEM_TOOL_NAMES,
    create_project_filesystem_middleware,
    create_project_skills_backend,
    create_project_skills_middleware,
)


def build_fast_agent(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    *,
    context_window_tokens: int = 128_000,
    compaction_trigger_ratio: float = 0.75,
    compaction_target_ratio: float = 0.15,
    token_counter: Callable[[Iterable[MessageLikeRepresentation]], int] | None = None,
    filesystem_backend: BackendProtocol | None = None,
    skills_backend: BackendProtocol | None = None,
    filesystem_tool_names: tuple[str, ...] = PROJECT_FILESYSTEM_TOOL_NAMES,
    tool_profiles: Sequence[ToolProfile] | None = None,
    visual_history_probe: VisualObservationHistoryProbe | None = None,
    live_view_resolver: Callable[[str, str, str], Any] | None = None,
    additional_middleware: Sequence[AgentMiddleware] = (),
    state_schema: type[FastAgentState] = FastAgentState,
    current_location: str | None = None,
    name: str = "AssistantFastAgent",
):
    """Build the shared create_agent unit without binding saver or Store."""

    if context_window_tokens < 100:
        raise ValueError("context window must contain at least 100 tokens")
    if not 0 < compaction_target_ratio < compaction_trigger_ratio <= 1:
        raise ValueError("compaction ratios must satisfy 0 < target < trigger <= 1")
    resolved_filesystem_backend = (
        filesystem_backend
        or create_project_skills_backend(Path(__file__).resolve().parents[3])
    )
    resolved_skills_backend = skills_backend or resolved_filesystem_backend
    skills_middleware = create_project_skills_middleware(resolved_skills_backend)
    filesystem_middleware = create_project_filesystem_middleware(
        resolved_filesystem_backend,
        tools=filesystem_tool_names,
    )
    filesystem_tools = tuple(filesystem_middleware.tools)
    resolved_tool_profiles = (
        project_tool_profiles() if tool_profiles is None else tuple(tool_profiles)
    )

    governed_tools = [*tools, *filesystem_tools]
    read_tool_names = _retryable_read_tool_names(governed_tools)
    interrupt_policy = {
        tool.name: {
            "allowed_decisions": ["approve", "edit", "reject", "respond"],
            "when": (
                _always_require_approval
                if (tool.metadata or {}).get("source") in {"deepagents", "mcp"}
                else _planning_mode_requires_approval
            ),
        }
        for tool in governed_tools
        if (tool.metadata or {}).get("effect") not in {None, "read"}
    }
    middleware = [
        create_assistant_base_prompt(),
        skills_middleware,
        filesystem_middleware,
        ToolProfileMiddleware(resolved_tool_profiles),
        ConditionalToolExposureMiddleware(
            visual_history_probe,
            live_view_resolver,
        ),
    ]
    tool_retry_middleware = (
        ToolRetryMiddleware(
            max_retries=2,
            tools=read_tool_names,
            initial_delay=0,
            backoff_factor=0,
            jitter=False,
        )
        if read_tool_names
        else None
    )
    middleware.append(
        PerToolCallLimitMiddleware(max_parallel_calls_per_tool=12)
    )
    summarization_options = {
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
    middleware.append(SummarizationMiddleware(**summarization_options))
    middleware.append(MemoryContextMiddleware())
    if interrupt_policy:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_policy))
    middleware.extend(additional_middleware)
    middleware.append(create_assistant_runtime_prompt(current_location))
    middleware.append(RecursionFinalSynthesisMiddleware())
    middleware.append(ToolProgressMiddleware())
    if tool_retry_middleware is not None:
        middleware.append(tool_retry_middleware)

    return create_agent(
        model=model,
        tools=list(tools),
        state_schema=state_schema,
        context_schema=AssistantRunContext,
        middleware=middleware,
        name=name,
    )


def _planning_mode_requires_approval(request: ToolCallRequest) -> bool:
    return request.state.get("execution_mode") == "planning"


def _always_require_approval(_request: ToolCallRequest) -> bool:
    return True


__all__ = [
    "MemoryContextMiddleware",
    "RecursionFinalSynthesisMiddleware",
    "RecursionFinalSynthesisState",
    "ToolProgressMiddleware",
    "_request_with_memory_context",
    "_retryable_read_tool_names",
    "_tool_progress_event",
    "build_fast_agent",
    "memory_context_message",
]

"""Runtime service for assistant context assembly and token preflight."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from assistant_agent.context.compactor import (
    ContextCompactor,
    context_summary_from_metadata,
    format_context_summary,
)
from assistant_agent.context.models import AssistantContextPack, ContextSummary
from assistant_agent.context.observability import build_traced_assistant_context_pack
from assistant_agent.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompileResult,
    PromptCompiler,
    prompt_tool_specs_for_mode,
)
from assistant_agent.context.token_budget import (
    ContextWindowDecision,
    ContextWindowPolicy,
)
from assistant_agent.context.token_counter import ContextTokenCounter
from assistant_agent.observability.trace_store import TraceStore
from assistant_agent.runtime.chat_adapter import ChatRequest
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.models import ToolSpec


@dataclass(frozen=True)
class AssistantDecisionContext:
    """Read-only context used by one assistant decision."""

    context_pack: AssistantContextPack
    request: UserRequest
    memory_summaries: list[str]
    memory_text: str
    tool_specs: list[ToolSpec]
    tool_observations: list[dict[str, Any]]
    iterations: int
    max_iterations: int
    is_mock: bool
    answer_only: bool = False


@dataclass(frozen=True)
class ContextPreflightFailure:
    """Stable failure returned when a Provider call must be blocked."""

    reason: str


@dataclass(frozen=True)
class ContextPreflightResult:
    """Compiled Provider request and the context that produced it."""

    request: ChatRequest
    context: AssistantDecisionContext
    failure: ContextPreflightFailure | None = None


@dataclass(frozen=True)
class _ConversationTurnForCompaction:
    user_text: str
    assistant_text: str
    run_id: str
    trace_id: str


class ContextService:
    """Own context construction, compilation, token preflight, and rebuilding."""

    def __init__(
        self,
        *,
        compactor: ContextCompactor | None = None,
        token_counter: ContextTokenCounter | None = None,
        window_policy: ContextWindowPolicy | None = None,
        current_location: str | None = None,
    ) -> None:
        if compactor is not None and token_counter is None:
            raise ValueError("context compaction requires a model tokenizer")
        self.compactor = compactor
        self.token_counter = token_counter
        self.window_policy = window_policy
        self.current_location = current_location

    def build(
        self,
        *,
        state: AgentState,
        request: UserRequest,
        observations: list[dict[str, Any]],
        tool_specs: list[ToolSpec],
        iteration: int,
        max_iterations: int,
        is_mock: bool,
        trace_store: TraceStore | None = None,
        trace_id: str | None = None,
        node_name: str = "assistant",
        registry_generation: str | None = None,
        host_configured_tool_names: set[str] | None = None,
        context_projector: Callable[[UserRequest], None] | None = None,
        native_calls: list[dict[str, Any]] | None = None,
        answer_only: bool = False,
    ) -> AssistantDecisionContext:
        if context_projector is not None:
            context_projector(request)
        pack = build_traced_assistant_context_pack(
            trace_store=trace_store,
            trace_id=trace_id,
            node_name=node_name,
            state=state,
            request=request,
            observations=observations,
            tool_specs=tool_specs,
            iteration=iteration,
            max_iterations=max_iterations,
            context_compactor=self.compactor,
            registry_generation=registry_generation,
            host_configured_tool_names=host_configured_tool_names,
            native_calls=native_calls,
            current_location=self.current_location,
            answer_only=answer_only,
        )
        return self._from_pack(
            pack,
            iterations=iteration,
            max_iterations=max_iterations,
            is_mock=is_mock,
            answer_only=answer_only,
        )

    def compile_native_request(
        self,
        context: AssistantDecisionContext,
        state: AgentState,
    ) -> PromptCompileResult:
        compilation = PromptCompiler().compile(
            PromptCompileRequest(
                user_id=state.user_id,
                session_id=state.session_id,
                mode=PromptCompileMode.NATIVE_TOOL,
                user_query_fallback="native_tools assistant turn",
                context_pack=context.context_pack,
                observations=tuple(context.tool_observations),
                native_calls=tuple(_native_tool_calls(state)),
                tool_call_id_prefix="call_",
                current_location=self.current_location,
                answer_only=context.answer_only,
            )
        )
        if compilation.selected_tool_specs:
            return compilation
        return PromptCompileResult(
            chat_request=compilation.chat_request.model_copy(
                update={"tool_choice": "none" if context.answer_only else None}
            ),
            system_instruction=compilation.system_instruction,
            rendered_context=compilation.rendered_context,
            selected_tool_specs=compilation.selected_tool_specs,
        )

    def preflight(
        self,
        context: AssistantDecisionContext,
        state: AgentState,
        *,
        force_hard: bool = False,
        trace_store: TraceStore | None = None,
        trace_id: str | None = None,
        node_name: str = "assistant",
        context_projector: Callable[[UserRequest], None] | None = None,
    ) -> ContextPreflightResult:
        request = self.compile_native_request(context, state).chat_request
        if (
            self.compactor is None
            or self.token_counter is None
            or self.window_policy is None
        ):
            return ContextPreflightResult(request=request, context=context)

        input_tokens = self.token_counter.count_chat_request(request)
        decision = self.window_policy.evaluate(
            input_tokens,
            reserved_output_tokens=request.max_tokens,
        )
        if force_hard:
            decision = ContextWindowDecision(
                input_tokens=decision.input_tokens,
                effective_input_limit=decision.effective_input_limit,
                usage_ratio=decision.usage_ratio,
                target_tokens=decision.target_tokens,
                triggered=True,
                hard=True,
            )
        _record_context_preflight(
            state,
            decision=decision,
            tokenizer_id=self.token_counter.tokenizer_id,
        )
        if not decision.triggered:
            return ContextPreflightResult(request=request, context=context)

        turns = _conversation_turns_for_compaction(context.request)
        if not turns:
            state.request.metadata["context_compaction_skipped_reason"] = (
                "no_completed_history"
            )
            if decision.hard:
                state.request.metadata["context_compaction_blocked"] = True
                return ContextPreflightResult(
                    request=request,
                    context=context,
                    failure=ContextPreflightFailure(
                        "上下文已进入硬限制区，但没有可压缩的已完成历史。"
                    ),
                )
            return ContextPreflightResult(request=request, context=context)

        context_without_history = _context_without_raw_history(context)
        fixed_request = self.compile_native_request(
            context_without_history,
            state,
        ).chat_request
        fixed_tokens = self.token_counter.count_chat_request(fixed_request)
        source_tokens = max(0, input_tokens - fixed_tokens)
        existing_summary = _existing_context_summary(context.request)
        existing_summary_tokens = (
            self.token_counter.count_text(existing_summary.summary_text)
            if existing_summary is not None
            else 0
        )
        summary_budget = min(
            self.window_policy.summary_max_tokens,
            max(
                512,
                decision.target_tokens - fixed_tokens + existing_summary_tokens,
            ),
        )

        attempts = 2 if decision.hard else 1
        last_error = ""
        for attempt in range(attempts):
            attempt_budget = max(512, summary_budget // (2**attempt))
            try:
                compacted = self.compactor.compact(
                    conversation=turns,
                    current_request=context.request,
                    observations=[],
                    budget_report=context.context_pack.budget,
                    existing_summary=existing_summary,
                    source_token_count=source_tokens,
                    summary_max_tokens=attempt_budget,
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                _record_context_compaction_usage(
                    context.request,
                    getattr(exc, "provider_usage", None),
                )
                last_error = (
                    "ValueError" if isinstance(exc, ValueError) else type(exc).__name__
                )
                continue

            _record_context_compaction_usage(
                context.request,
                compacted.provider_usage,
            )
            compacted_context = _rolling_summary_context(
                context,
                compacted.summary,
                compactor_type=compacted.compactor_type,
            )
            compacted_request = self.compile_native_request(
                compacted_context,
                state,
            ).chat_request
            compacted_tokens = self.token_counter.count_chat_request(
                compacted_request
            )
            compacted_decision = self.window_policy.evaluate(
                compacted_tokens,
                reserved_output_tokens=compacted_request.max_tokens,
            )
            metadata = compacted_context.request.metadata
            metadata["context_compaction_output_tokens"] = compacted_tokens
            metadata["context_compaction_target_tokens"] = decision.target_tokens
            metadata["context_compaction_target_reached"] = (
                compacted_tokens <= decision.target_tokens
            )
            metadata["context_compaction_attempts"] = attempt + 1
            if not compacted_decision.hard:
                before_compaction = metadata.get("context_token_preflight")
                if isinstance(before_compaction, dict):
                    metadata["context_token_preflight_before_compaction"] = dict(
                        before_compaction
                    )
                metadata["context_token_preflight"] = _context_preflight_payload(
                    compacted_decision,
                    tokenizer_id=self.token_counter.tokenizer_id,
                )
                state.request = compacted_context.request
                compacted_context = self._rebuild(
                    compacted_context,
                    state=state,
                    trace_store=trace_store,
                    trace_id=trace_id,
                    node_name=node_name,
                    context_projector=context_projector,
                    build_reason=(
                        "provider_overflow_retry"
                        if force_hard
                        else "post_compaction"
                    ),
                )
                state.request = compacted_context.request
                return ContextPreflightResult(
                    request=self.compile_native_request(
                        compacted_context,
                        state,
                    ).chat_request,
                    context=compacted_context,
                )
            last_error = "compacted_context_still_hard"

        metadata = state.request.metadata
        metadata["context_compaction_failed"] = True
        metadata["context_compaction_error_code"] = last_error or "unknown"
        if decision.hard:
            metadata["context_compaction_blocked"] = True
            return ContextPreflightResult(
                request=request,
                context=context,
                failure=ContextPreflightFailure(
                    "上下文压缩失败，继续调用可能超过模型上下文限制。"
                ),
            )
        return ContextPreflightResult(request=request, context=context)

    def selected_tool_specs(
        self,
        context: AssistantDecisionContext,
    ) -> list[ToolSpec]:
        return list(
            prompt_tool_specs_for_mode(
                context.context_pack,
                PromptCompileMode.NATIVE_TOOL,
            )
        )

    def _rebuild(
        self,
        context: AssistantDecisionContext,
        *,
        state: AgentState,
        trace_store: TraceStore | None,
        trace_id: str | None,
        node_name: str,
        context_projector: Callable[[UserRequest], None] | None,
        build_reason: str,
    ) -> AssistantDecisionContext:
        if context_projector is not None:
            context_projector(context.request)
        pack = build_traced_assistant_context_pack(
            trace_store=trace_store,
            trace_id=trace_id,
            node_name=node_name,
            state=state,
            request=context.request,
            observations=context.tool_observations,
            tool_specs=context.tool_specs,
            iteration=context.iterations,
            max_iterations=context.max_iterations,
            memory_text=context.memory_text,
            context_compactor=self.compactor,
            native_calls=_native_tool_calls(state),
            current_location=self.current_location,
            answer_only=context.answer_only,
            build_reason=build_reason,
        )
        return self._from_pack(
            pack,
            iterations=context.iterations,
            max_iterations=context.max_iterations,
            is_mock=context.is_mock,
            answer_only=context.answer_only,
        )

    @staticmethod
    def _from_pack(
        pack: AssistantContextPack,
        *,
        iterations: int,
        max_iterations: int,
        is_mock: bool,
        answer_only: bool = False,
    ) -> AssistantDecisionContext:
        return AssistantDecisionContext(
            context_pack=pack,
            request=pack.request,
            memory_summaries=pack.memory_summaries,
            memory_text=pack.memory_text,
            tool_specs=pack.tool_specs,
            tool_observations=pack.observations,
            iterations=iterations,
            max_iterations=max_iterations,
            is_mock=is_mock,
            answer_only=answer_only,
        )


def _record_context_preflight(
    state: AgentState,
    *,
    decision: ContextWindowDecision,
    tokenizer_id: str,
) -> None:
    state.request.metadata["context_token_preflight"] = _context_preflight_payload(
        decision,
        tokenizer_id=tokenizer_id,
    )


def _record_context_compaction_usage(request: UserRequest, usage: Any) -> None:
    if not isinstance(usage, dict) or not usage:
        return
    history = request.metadata.setdefault(
        "context_compaction_provider_usage_history",
        [],
    )
    if isinstance(history, list):
        history.append(dict(usage))
        del history[:-10]


def _context_preflight_payload(
    decision: ContextWindowDecision,
    *,
    tokenizer_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": "context_token_preflight_v1",
        "tokenizer_id": tokenizer_id,
        "input_tokens": decision.input_tokens,
        "effective_input_limit": decision.effective_input_limit,
        "usage_ratio": decision.usage_ratio,
        "target_tokens": decision.target_tokens,
        "triggered": decision.triggered,
        "hard": decision.hard,
    }


def _conversation_turns_for_compaction(
    request: UserRequest,
) -> list[_ConversationTurnForCompaction]:
    history = request.metadata.get("conversation_history")
    if not isinstance(history, list):
        return []
    turns: list[_ConversationTurnForCompaction] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        user_text = item.get("user_text")
        assistant_text = item.get("assistant_text")
        if not isinstance(user_text, str) or not isinstance(assistant_text, str):
            continue
        turns.append(
            _ConversationTurnForCompaction(
                user_text=user_text,
                assistant_text=assistant_text,
                run_id=str(item.get("run_id") or ""),
                trace_id=str(item.get("trace_id") or ""),
            )
        )
    return turns


def _existing_context_summary(request: UserRequest) -> ContextSummary | None:
    summary = context_summary_from_metadata(request.metadata.get("context_summary"))
    if summary is not None:
        return summary
    return context_summary_from_metadata(
        request.metadata.get("session_context_summary")
    )


def _context_without_raw_history(
    context: AssistantDecisionContext,
) -> AssistantDecisionContext:
    metadata = dict(context.request.metadata)
    metadata.update(
        {
            "conversation_history": [],
            "conversation_context_recent_turns": 0,
            "conversation_context_recent_tokens": 0,
            "conversation_context_recent_token_budget": 0,
        }
    )
    request = context.request.model_copy(
        update={"metadata": metadata},
        deep=True,
    )
    pack = context.context_pack.model_copy(
        update={
            "request": request,
            "conversation_text": "",
        },
        deep=True,
    )
    return AssistantDecisionContext(
        context_pack=pack,
        request=request,
        memory_summaries=context.memory_summaries,
        memory_text=context.memory_text,
        tool_specs=context.tool_specs,
        tool_observations=context.tool_observations,
        iterations=context.iterations,
        max_iterations=context.max_iterations,
        is_mock=context.is_mock,
        answer_only=context.answer_only,
    )


def _rolling_summary_context(
    context: AssistantDecisionContext,
    summary: ContextSummary,
    *,
    compactor_type: str,
) -> AssistantDecisionContext:
    metadata = dict(context.request.metadata)
    rendered_summary = format_context_summary(summary)
    metadata.update(
        {
            "conversation_history": [],
            "conversation_context_text": "",
            "conversation_context_recent_turns": 0,
            "conversation_context_recent_tokens": 0,
            "conversation_context_recent_token_budget": 0,
            "conversation_context_token_aware": True,
            "conversation_context_compacted": True,
            "conversation_context_compacted_turns": summary.covered_turn_count,
            "context_summary": summary.model_dump(mode="json"),
            "session_context_summary": summary.model_dump(mode="json"),
            "context_summary_text": rendered_summary,
            "context_summary_present": True,
            "context_compactor_type": compactor_type,
            "context_compaction_applied": True,
        }
    )
    request = context.request.model_copy(
        update={"metadata": metadata},
        deep=True,
    )
    return AssistantDecisionContext(
        context_pack=context.context_pack.model_copy(
            update={
                "request": request,
                "context_summary": summary,
                "compactor_type": compactor_type,
                "conversation_text": "",
            },
            deep=True,
        ),
        request=request,
        memory_summaries=context.memory_summaries,
        memory_text=context.memory_text,
        tool_specs=context.tool_specs,
        tool_observations=context.tool_observations,
        iterations=context.iterations,
        max_iterations=context.max_iterations,
        is_mock=context.is_mock,
        answer_only=context.answer_only,
    )


def _native_tool_calls(state: AgentState) -> list[dict[str, Any]]:
    calls = state.request.metadata.get("native_tool_calls", [])
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]

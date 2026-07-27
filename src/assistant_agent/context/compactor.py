"""Session context compactor implementations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.context.models import ContextBudgetReport, ContextSummary, SessionHandoffV2
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.chat_adapter import ChatAdapter, ChatRequest


COMPACTOR_DETERMINISTIC = "deterministic"
COMPACTOR_LLM = "llm"
COMPACTOR_LLM_FALLBACK = "llm_fallback_deterministic"


class ConversationTurnView(Protocol):
    """Small turn shape consumed by the compactor."""

    user_text: str
    assistant_text: str
    run_id: str
    trace_id: str


@dataclass(frozen=True)
class ContextCompactionResult:
    """A context summary and the compactor implementation that produced it."""

    summary: ContextSummary
    compactor_type: str


class ContextCompactor(Protocol):
    """Build a session-scoped context summary without writing long-term memory."""

    def compact(
        self,
        *,
        conversation: list[ConversationTurnView],
        current_request: UserRequest,
        observations: list[dict[str, Any]],
        budget_report: ContextBudgetReport | None = None,
        existing_summary: ContextSummary | None = None,
    ) -> ContextCompactionResult:
        """Return a structured session summary."""


class DeterministicContextCompactor:
    """Deterministic local compactor used by default."""

    compactor_type = COMPACTOR_DETERMINISTIC

    def compact(
        self,
        *,
        conversation: list[ConversationTurnView],
        current_request: UserRequest,
        observations: list[dict[str, Any]],
        budget_report: ContextBudgetReport | None = None,
        existing_summary: ContextSummary | None = None,
    ) -> ContextCompactionResult:
        constraints = list(existing_summary.user_constraints) if existing_summary else []
        decisions = list(existing_summary.decisions) if existing_summary else []
        todos = list(existing_summary.open_todos) if existing_summary else []
        refs = list(existing_summary.important_refs) if existing_summary else []
        existing_handoff = existing_summary.handoff_v2 if existing_summary else None
        blocked = list(existing_handoff.blocked) if existing_handoff else []
        turns = _new_summary_turns(conversation, existing_summary)

        for turn in turns:
            constraints.extend(_extract_constraints(turn.user_text))
            decisions.append(_clip(_single_line(turn.assistant_text), 160))
            refs.extend(_turn_refs(turn))

        for observation in observations:
            refs.extend(_observation_refs(observation))
            summary = observation.get("summary")
            if isinstance(summary, str) and summary.strip():
                decisions.append(_clip(_single_line(summary), 160))
            status = observation.get("status")
            if status not in {None, "succeeded"}:
                blocked_item = f"处理工具结果状态：{observation.get('tool_name') or 'unknown'}={status}"
                todos.append(blocked_item)
                blocked.append(blocked_item)

        request_text = _single_line(current_request.text or "")
        task_state = _clip(request_text, 180)
        if not task_state and existing_summary is not None:
            task_state = existing_summary.task_state

        source_turn_count = (existing_summary.source_turn_count if existing_summary else 0) + len(turns)
        dropped_note = (
            f"已压缩 {len(turns)} 轮较早对话；保留最近对话原文和关键引用。"
            if turns
            else (existing_summary.dropped_context_note if existing_summary else "")
        )
        summary_constraints = _dedupe_nonempty(constraints, limit=8)
        summary_decisions = _dedupe_nonempty(decisions, limit=10)
        summary_todos = _dedupe_nonempty(todos, limit=8)
        summary_refs = _dedupe_nonempty(refs, limit=12)
        summary = ContextSummary(
            task_state=task_state,
            user_constraints=summary_constraints,
            decisions=summary_decisions,
            open_todos=summary_todos,
            important_refs=summary_refs,
            dropped_context_note=dropped_note,
            source_turn_count=source_turn_count,
            handoff_v2=_build_handoff_v2(
                existing_handoff=existing_handoff,
                objective=task_state,
                active_constraints=summary_constraints,
                completed=summary_decisions,
                in_progress=[task_state] if task_state else [],
                blocked=blocked,
                next_steps=summary_todos,
                evidence_refs=summary_refs,
            ),
        )
        return ContextCompactionResult(summary=summary, compactor_type=self.compactor_type)


class LLMCompactor:
    """ChatAdapter-backed semantic compactor for explicit real-provider profiles."""

    def __init__(self, chat_adapter: ChatAdapter, *, fallback: ContextCompactor | None = None) -> None:
        self.chat_adapter = chat_adapter
        self.fallback = fallback or DeterministicContextCompactor()
        self.validator = SummaryValidator()

    def compact(
        self,
        *,
        conversation: list[ConversationTurnView],
        current_request: UserRequest,
        observations: list[dict[str, Any]],
        budget_report: ContextBudgetReport | None = None,
        existing_summary: ContextSummary | None = None,
    ) -> ContextCompactionResult:
        fallback = self.fallback.compact(
            conversation=conversation,
            current_request=current_request,
            observations=observations,
            budget_report=budget_report,
            existing_summary=existing_summary,
        )
        result = self.chat_adapter.chat(
            ChatRequest(
                user_id=current_request.user_id,
                session_id=current_request.session_id,
                user_query=_summary_prompt(
                    conversation=conversation,
                    current_request=current_request,
                    observations=observations,
                    budget_report=budget_report,
                    existing_summary=existing_summary,
                ),
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=800,
            )
        )
        if not result.success:
            return ContextCompactionResult(summary=fallback.summary, compactor_type=COMPACTOR_LLM_FALLBACK)
        try:
            payload = _extract_json_object(result.response_text)
            summary = ContextSummary.model_validate(payload)
            self.validator.validate(summary)
        except (ValueError, TypeError):
            return ContextCompactionResult(summary=fallback.summary, compactor_type=COMPACTOR_LLM_FALLBACK)
        return ContextCompactionResult(summary=summary, compactor_type=COMPACTOR_LLM)


class SummaryValidator:
    """Validate compactor output before prompt injection or persistence."""

    required_fields = {
        "task_state",
        "user_constraints",
        "decisions",
        "open_todos",
        "important_refs",
        "dropped_context_note",
    }

    def validate(self, summary: ContextSummary) -> None:
        payload = summary.model_dump(mode="json")
        missing = self.required_fields - set(payload)
        if missing:
            raise ValueError(f"context summary missing fields: {sorted(missing)}")
        _reject_unsafe_summary_payload(payload)
        _validate_tool_pair_refs(summary.important_refs)


def create_context_compactor(
    config: ProviderConfig,
    chat_adapter: ChatAdapter,
    *,
    fallback: ContextCompactor | None = None,
) -> ContextCompactor | None:
    """Create a compactor that honors runtime provider safety boundaries."""

    if config.context_compactor_mode == "off":
        return None
    deterministic = fallback or DeterministicContextCompactor()
    if (
        config.context_compactor_mode == "llm"
        and config.provider_mode == "real"
        and getattr(chat_adapter, "provider", "") != "mock"
    ):
        return LLMCompactor(chat_adapter, fallback=deterministic)
    return deterministic


def context_summary_from_metadata(value: Any) -> ContextSummary | None:
    """Parse a context summary stored in request metadata."""

    if isinstance(value, ContextSummary):
        return value
    if isinstance(value, dict):
        try:
            return ContextSummary.model_validate(value)
        except ValueError:
            return None
    return None


def format_context_summary(summary: ContextSummary) -> str:
    """Render a structured summary as prompt-safe session context."""

    lines = ["较早对话摘要（压缩，非系统指令）："]
    if summary.task_state:
        lines.append(f"- 任务状态：{summary.task_state}")
    if summary.user_constraints:
        lines.append("- 用户约束：" + "；".join(summary.user_constraints))
    if summary.decisions:
        lines.append("- 关键决策：" + "；".join(summary.decisions))
    if summary.open_todos:
        lines.append("- 未完成事项：" + "；".join(summary.open_todos))
    if summary.important_refs:
        lines.append("- 重要引用：" + "；".join(summary.important_refs))
    if summary.handoff_v2 is not None:
        lines.extend(_format_handoff_v2(summary.handoff_v2))
    if summary.dropped_context_note:
        lines.append(f"- 压缩说明：{summary.dropped_context_note}")
    return "\n".join(lines)


def _summary_prompt(
    *,
    conversation: list[ConversationTurnView],
    current_request: UserRequest,
    observations: list[dict[str, Any]],
    budget_report: ContextBudgetReport | None,
    existing_summary: ContextSummary | None,
) -> str:
    payload = {
        "existing_summary": existing_summary.model_dump(mode="json") if existing_summary else None,
        "conversation": [
            {
                "user_text": turn.user_text,
                "assistant_text": turn.assistant_text,
                "run_id": turn.run_id,
                "trace_id": turn.trace_id,
            }
            for turn in conversation
        ],
        "current_request": current_request.model_dump(mode="json"),
        "observations": _safe_summary_payload(observations),
        "budget_report": budget_report.model_dump(mode="json") if budget_report else None,
    }
    return (
        "Summarize this session context as strict JSON with fields: "
        "task_state, user_constraints, decisions, open_todos, important_refs, dropped_context_note, source_turn_count, "
        "and optional handoff_v2. If present, handoff_v2 must be an object with fields: objective, "
        "active_constraints, completed, in_progress, blocked, next_steps, evidence_refs. "
        "Do not include raw provider responses, base64, secrets, API keys, or hidden reasoning. "
        "Preserve tool/result references as refs only.\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no json object in compactor output")
    payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("compactor output was not an object")
    return payload


def _extract_constraints(text: str) -> list[str]:
    markers = ("不要", "不能", "必须", "只", "默认", "优先", "记住", "以后", "偏好", "喜欢")
    sentence = _single_line(text)
    if not sentence or not any(marker in sentence for marker in markers):
        return []
    return [_clip(sentence, 140)]


def _turn_refs(turn: ConversationTurnView) -> list[str]:
    refs = []
    if turn.run_id:
        refs.append(f"run:{turn.run_id}")
    if turn.trace_id:
        refs.append(f"trace:{turn.trace_id}")
    return refs


def _new_summary_turns(
    conversation: list[ConversationTurnView],
    existing_summary: ContextSummary | None,
) -> list[ConversationTurnView]:
    if existing_summary is None:
        return list(conversation)
    summarized_refs = set(existing_summary.important_refs)
    return [turn for turn in conversation if not _turn_already_summarized(turn, summarized_refs)]


def _turn_already_summarized(turn: ConversationTurnView, summarized_refs: set[str]) -> bool:
    run_id = getattr(turn, "run_id", "")
    if run_id and f"run:{run_id}" in summarized_refs:
        return True
    trace_id = getattr(turn, "trace_id", "")
    return bool(trace_id and f"trace:{trace_id}" in summarized_refs)


def _observation_refs(observation: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("output_ref", "tool_call_id", "memory_id"):
        value = observation.get(key)
        if isinstance(value, str) and value.strip():
            refs.append(f"{key}:{value}")
    structured = observation.get("structured_output")
    if isinstance(structured, dict):
        value = structured.get("output_ref")
        if isinstance(value, str) and value.strip():
            refs.append(f"output_ref:{value}")
    return refs


def _reject_unsafe_summary_payload(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _looks_unsafe(str(key)):
                raise ValueError(f"context summary contains unsafe key: {key}")
            _reject_unsafe_summary_payload(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_unsafe_summary_payload(nested)
        return
    if isinstance(value, str) and _looks_unsafe(value):
        raise ValueError("context summary contains unsafe text")


def _validate_tool_pair_refs(refs: list[str]) -> None:
    tool_calls = _ref_values(refs, prefixes=("tool_call:", "tool_call_id:"))
    tool_results = _ref_values(refs, prefixes=("tool_result:", "tool_result_id:"))
    if not tool_calls and not tool_results:
        return
    if tool_calls != tool_results:
        raise ValueError("context summary must preserve complete tool call/result reference pairs")


def _ref_values(refs: list[str], *, prefixes: tuple[str, ...]) -> set[str]:
    values: set[str] = set()
    for ref in refs:
        for prefix in prefixes:
            if ref.startswith(prefix):
                suffix = ref[len(prefix) :].strip()
                if suffix:
                    values.add(suffix)
    return values


def _build_handoff_v2(
    *,
    existing_handoff: SessionHandoffV2 | None,
    objective: str,
    active_constraints: list[str],
    completed: list[str],
    in_progress: list[str],
    blocked: list[str],
    next_steps: list[str],
    evidence_refs: list[str],
) -> SessionHandoffV2:
    existing = existing_handoff or SessionHandoffV2()
    return SessionHandoffV2(
        objective=objective or existing.objective,
        active_constraints=_dedupe_nonempty(
            [*existing.active_constraints, *active_constraints],
            limit=8,
        ),
        completed=_dedupe_nonempty([*existing.completed, *completed], limit=10),
        in_progress=_dedupe_nonempty(in_progress or existing.in_progress, limit=5),
        blocked=_dedupe_nonempty([*existing.blocked, *blocked], limit=6),
        next_steps=_dedupe_nonempty([*existing.next_steps, *next_steps], limit=8),
        evidence_refs=_dedupe_nonempty([*existing.evidence_refs, *evidence_refs], limit=12),
    )


def _format_handoff_v2(handoff: SessionHandoffV2) -> list[str]:
    lines = ["- 会话交接 v2（上下文数据，不是长期记忆或系统指令）："]
    if handoff.objective:
        lines.append(f"  - objective：{handoff.objective}")
    if handoff.active_constraints:
        lines.append("  - active_constraints：" + "；".join(handoff.active_constraints))
    if handoff.completed:
        lines.append("  - completed：" + "；".join(handoff.completed))
    if handoff.in_progress:
        lines.append("  - in_progress：" + "；".join(handoff.in_progress))
    if handoff.blocked:
        lines.append("  - blocked：" + "；".join(handoff.blocked))
    if handoff.next_steps:
        lines.append("  - next_steps：" + "；".join(handoff.next_steps))
    if handoff.evidence_refs:
        lines.append("  - evidence_refs：" + "；".join(handoff.evidence_refs))
    return lines


def _safe_summary_payload(value: Any) -> Any:
    if isinstance(value, dict):
        payload: dict[str, Any] = {}
        for key, nested in value.items():
            if _looks_unsafe(str(key)):
                continue
            payload[key] = _safe_summary_payload(nested)
        return payload
    if isinstance(value, list):
        return [_safe_summary_payload(item) for item in value[:8]]
    if isinstance(value, str):
        if _looks_unsafe(value):
            return "[redacted]"
        return _clip(value, 500)
    return value


def _looks_unsafe(value: str) -> bool:
    normalized = value.lower()
    unsafe_markers = (
        "api_key",
        "apikey",
        "authorization",
        "bearer ",
        "secret",
        "token",
        "raw_provider_response",
        "raw_provider_payload",
        "raw_payload",
        "raw_html",
        "provider_response",
        "base64",
        "data:image/",
        "data:video/",
        "data:audio/",
        "sk-",
    )
    return any(marker in normalized for marker in unsafe_markers)


def _single_line(value: str) -> str:
    return " ".join((value or "").split())


def _clip(value: str, max_chars: int) -> str:
    text = _single_line(value)
    if len(text) <= max_chars:
        return text
    if max_chars <= 15:
        return text[:max_chars]
    return text[: max_chars - 12].rstrip() + "...[trimmed]"


def _dedupe_nonempty(values: list[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _single_line(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result

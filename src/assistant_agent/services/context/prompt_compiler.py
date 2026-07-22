"""Compile governed assistant context into provider-native chat requests."""

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any

from assistant_agent.agent.system_prompt_policy import (
    SystemPromptOptions,
    SystemPromptProfile,
    render_system_instruction,
)
from assistant_agent.schemas.context import AssistantContextPack, RenderedAssistantContext
from assistant_agent.schemas.tool_spec_adapters import tool_specs_to_openai_tools
from assistant_agent.schemas.tools import ToolSpec
from assistant_agent.services.chat_adapter import ChatRequest, ChatStreamCallback
from assistant_agent.services.context.renderer import render_native_tool_context

_ASSISTANT_REASONING_CONTENT_KEY = "assistant_reasoning_content"
_ASSISTANT_TURN_ID_KEY = "assistant_turn_id"
class PromptCompileMode(StrEnum):
    """Supported production provider-request compilation modes."""

    NATIVE_TOOL = "native_tool"


@dataclass(frozen=True)
class PromptCompileRequest:
    """Inputs needed to compile one provider-native chat request."""

    user_id: str
    session_id: str
    mode: PromptCompileMode
    user_query_fallback: str
    profile: SystemPromptProfile
    options: SystemPromptOptions
    context_pack: AssistantContextPack
    observations: tuple[dict[str, Any], ...]
    native_calls: tuple[dict[str, Any], ...]
    tool_call_id_prefix: str
    stream_callback: ChatStreamCallback | None = None
    temperature: float = 0.2
    max_tokens: int = 1024


@dataclass(frozen=True)
class PromptCompileResult:
    """Compiled request plus prompt-safe materials used by observability."""

    chat_request: ChatRequest
    system_instruction: str
    rendered_context: RenderedAssistantContext
    selected_tool_specs: tuple[ToolSpec, ...]


class PromptCompiler:
    """Deterministically assemble an existing context pack for a provider."""

    def compile(self, request: PromptCompileRequest) -> PromptCompileResult:
        system_instruction = render_system_instruction(
            request.profile,
            options=request.options,
            owner_persona=owner_persona_for_pack(request.context_pack),
        )
        rendered_context = _render_context(request)
        user_content = _rendered_user_content(rendered_context, request.mode)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_instruction},
        ]
        messages.append({"role": "user", "content": user_content})
        messages.extend(_native_tool_messages(request))

        selected_tool_specs = prompt_tool_specs_for_mode(
            request.context_pack,
            request.mode,
        )
        user_query = request.context_pack.request.text or request.user_query_fallback
        chat_request = ChatRequest(
            user_id=request.user_id,
            session_id=request.session_id,
            user_query=user_query,
            messages=messages,
            tools=tool_specs_to_openai_tools(selected_tool_specs),
            tool_choice="auto" if selected_tool_specs else None,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream_callback=request.stream_callback,
        )
        return PromptCompileResult(
            chat_request=chat_request,
            system_instruction=system_instruction,
            rendered_context=rendered_context,
            selected_tool_specs=selected_tool_specs,
        )


def prompt_tool_specs_for_mode(
    pack: AssistantContextPack,
    mode: PromptCompileMode,
) -> tuple[ToolSpec, ...]:
    """Return the already-governed tool subset exposed for this mode."""

    if pack.run_tool_catalog.available_tool_names:
        return tuple(pack.prompt_tool_specs)
    return tuple(pack.prompt_tool_specs or pack.tool_specs)


def owner_persona_for_pack(pack: AssistantContextPack) -> str:
    """Return the single validated owner persona, or fail closed."""

    sections = [
        section
        for section in pack.context_sections
        if section.kind == "soul" and not section.sensitive
    ]
    if len(sections) != 1:
        return ""
    return sections[0].content


def _render_context(request: PromptCompileRequest) -> RenderedAssistantContext:
    return render_native_tool_context(request.context_pack)


def _rendered_user_content(
    rendered: RenderedAssistantContext,
    mode: PromptCompileMode,
) -> str:
    return rendered.native_user_message or ""


def _native_tool_messages(request: PromptCompileRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    index = 0
    while index < len(request.observations):
        call = _native_call_at(request, index)
        turn_id = _assistant_turn_id(call)
        if turn_id:
            grouped: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
            while index < len(request.observations):
                candidate = _native_call_at(request, index)
                if _assistant_turn_id(candidate) != turn_id:
                    break
                grouped.append((index, candidate, request.observations[index]))
                index += 1
            messages.extend(
                _native_tool_turn_messages(
                    grouped,
                    id_prefix=request.tool_call_id_prefix,
                )
            )
            continue
        messages.extend(
            _native_tool_turn_messages(
                [(index, call, request.observations[index])],
                id_prefix=request.tool_call_id_prefix,
            )
        )
        index += 1
    return messages


def _native_call_at(request: PromptCompileRequest, index: int) -> dict[str, Any]:
    return request.native_calls[index] if index < len(request.native_calls) else {}


def _assistant_turn_id(call: dict[str, Any]) -> str | None:
    value = call.get(_ASSISTANT_TURN_ID_KEY)
    return value if isinstance(value, str) and value else None


def _native_tool_turn_messages(
    grouped: list[tuple[int, dict[str, Any], dict[str, Any]]],
    *,
    id_prefix: str,
) -> list[dict[str, Any]]:
    tool_call_payloads = [
        _native_tool_call_payload(
            call,
            observation,
            index,
            id_prefix=id_prefix,
        )
        for index, call, observation in grouped
    ]
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": tool_call_payloads,
    }
    reasoning_content = _assistant_reasoning_content([call for _, call, _ in grouped])
    if reasoning_content is not None:
        assistant_message["reasoning_content"] = reasoning_content
    messages = [assistant_message]
    for payload, (_, _, observation) in zip(tool_call_payloads, grouped, strict=True):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": payload["id"],
                "name": payload["function"]["name"],
                "content": json.dumps(observation, ensure_ascii=False),
            }
        )
    return messages


def _assistant_reasoning_content(calls: list[dict[str, Any]]) -> str | None:
    for call in calls:
        value = call.get(_ASSISTANT_REASONING_CONTENT_KEY)
        if isinstance(value, str) and value:
            return value
    return None


def _native_tool_call_payload(
    call: dict[str, Any],
    observation: dict[str, Any],
    index: int,
    *,
    id_prefix: str,
) -> dict[str, Any]:
    call_id = str(call.get("id") or f"{id_prefix}{index + 1}")
    name = str(call.get("name") or observation.get("tool_name") or "unknown")
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    raw = call.get("raw") if isinstance(call.get("raw"), dict) else {}
    payload = dict(raw)
    raw_function = payload.get("function")
    function = dict(raw_function) if isinstance(raw_function, dict) else {}
    function.update(
        {
            "name": str(function.get("name") or name),
            "arguments": _arguments_json(function.get("arguments"), arguments),
        }
    )
    payload.update(
        {
            "id": str(payload.get("id") or call_id),
            "type": payload.get("type") or "function",
            "function": function,
        }
    )
    return payload


def _arguments_json(value: Any, fallback: dict[str, Any]) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return json.dumps(fallback, ensure_ascii=False)

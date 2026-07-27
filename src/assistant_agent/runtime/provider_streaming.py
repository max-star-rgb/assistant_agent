"""Runtime-local consumption of provider LLM event streams."""

from __future__ import annotations

import asyncio
import threading
from time import perf_counter
from typing import Any

from assistant_agent.providers.llm_events import LLMEvent, LLMEventAccumulator
from assistant_agent.runtime.chat_adapter import (
    ChatProviderError,
    ChatRequest,
    ChatResult,
    ProviderProtocolResponse,
    ProviderProtocolToolCall,
)


def supports_async_streaming_chat(adapter: object) -> bool:
    """Return whether an adapter exposes the optional async stream boundary."""

    return callable(getattr(adapter, "stream_chat", None))


class ProviderStreamingTurnRunner:
    """Run one provider stream turn and return the existing ChatResult shape."""

    def run_turn(self, adapter: object, request: ChatRequest) -> ChatResult:
        """Synchronously consume an async provider stream for the current runtime."""

        return _run_coro_sync(self._run_turn_async(adapter, request))

    async def _run_turn_async(self, adapter: object, request: ChatRequest) -> ChatResult:
        started_at = perf_counter()
        accumulator = LLMEventAccumulator()
        provider = str(getattr(adapter, "provider", "unknown") or "unknown")
        model = getattr(adapter, "model", None)
        refusal: str | None = None
        terminal_seen = False
        token_delta_count = 0
        tool_call_delta_count = 0
        reasoning_delta_count = 0

        stream_chat = getattr(adapter, "stream_chat")
        async for event in stream_chat(request):
            provider = event.provider
            if event.model is not None:
                model = event.model

            if event.event_type == "error":
                terminal_seen = True
                return _chat_result_from_error_event(
                    event,
                    provider=provider,
                    model=model,
                    latency_ms=_elapsed_ms(started_at),
                )

            accumulator.apply(event)
            if event.event_type == "token_delta":
                token_delta_count += 1
                _emit_token_delta_callback(request, event)
            elif event.event_type == "tool_call_delta":
                tool_call_delta_count += 1
            elif event.event_type == "reasoning_delta":
                reasoning_delta_count += 1
            elif event.event_type == "completed":
                terminal_seen = True
                raw_refusal = event.metadata.get("refusal")
                if isinstance(raw_refusal, str) and raw_refusal:
                    refusal = raw_refusal

        if not terminal_seen:
            return ChatResult(
                response_text="",
                provider=provider,
                model=model,
                latency_ms=_elapsed_ms(started_at),
                errors=[
                    ChatProviderError(
                        code="provider_bad_response",
                        message="chat provider stream ended without a terminal event",
                        recoverable=False,
                    )
                ],
            )

        response_text = accumulator.response_text
        tool_calls = accumulator.finalize_tool_calls(provider_format="openai_compatible")
        result_provider = accumulator.provider or provider
        result = ChatResult(
            response_text=response_text,
            tool_calls=tool_calls,
            reasoning_content=accumulator.reasoning_content or None,
            finish_reason=accumulator.finish_reason,
            refusal=refusal,
            provider=result_provider,
            model=accumulator.model or model,
            usage=accumulator.usage,
            latency_ms=_elapsed_ms(started_at),
            output_ref=f"provider://chat/{result_provider}",
            protocol_response=ProviderProtocolResponse(
                transport_mode="provider_stream",
                content=response_text,
                tool_calls=[
                    ProviderProtocolToolCall(
                        id=call.id,
                        type=str(call.raw.get("type")) if call.raw.get("type") is not None else None,
                        name=call.name,
                        arguments_raw=str(
                            (call.raw.get("function") or {}).get("arguments", "")
                            if isinstance(call.raw.get("function"), dict)
                            else ""
                        ),
                    )
                    for call in tool_calls
                ],
                refusal=refusal,
                finish_reason=accumulator.finish_reason,
                usage=accumulator.usage,
                token_delta_count=token_delta_count,
                tool_call_delta_count=tool_call_delta_count,
                reasoning_delta_count=reasoning_delta_count,
                terminal_seen=terminal_seen,
            ),
        )
        if (
            result.tool_calls
            or result.refusal
            or result.response_text.strip()
            or result.finish_reason == "length"
        ):
            return result
        return result.model_copy(
            update={
                "errors": [
                    ChatProviderError(
                        code="provider_empty_response",
                        message="chat provider returned empty content",
                        recoverable=True,
                    )
                ]
            }
        )


def _emit_token_delta_callback(request: ChatRequest, event: LLMEvent) -> None:
    if request.stream_callback is None or not event.text:
        return
    payload = dict(event.metadata)
    payload.setdefault("token_streaming", True)
    payload.setdefault("chunking_strategy", "provider_token_delta")
    payload["provider"] = event.provider
    if event.model is not None:
        payload["model"] = event.model
    request.stream_callback(event.text, payload)


def _chat_result_from_error_event(
    event: LLMEvent,
    *,
    provider: str,
    model: str | None,
    latency_ms: int,
) -> ChatResult:
    error = event.error
    return ChatResult(
        response_text="",
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        errors=[
            ChatProviderError(
                code=error.code if error is not None else "provider_unknown_error",
                message=error.message if error is not None else "Chat provider error.",
                recoverable=error.recoverable if error is not None else False,
            )
        ],
    )


def _elapsed_ms(started_at: float) -> int:
    return int((perf_counter() - started_at) * 1000)


def _run_coro_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def worker() -> None:
        try:
            result_box["result"] = asyncio.run(coro)
        except BaseException as exc:
            error_box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in error_box:
        raise error_box["error"]
    return result_box["result"]

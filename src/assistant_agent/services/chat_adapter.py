"""Direct chat adapter contracts and local implementations."""

from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
import inspect
import time
from typing import Any, Literal, Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import NativeToolCall, openai_tool_call_to_native_tool_call
from assistant_agent.schemas.llm_events import LLMEvent, LLMEventAccumulator, LLMProviderError, LLMToolCallDelta
from assistant_agent.schemas.provider_specs import CHAT_PROVIDER_SPECS, ProviderCapabilities
from assistant_agent.services.provider_errors import ProviderAdapterError, build_provider_error
from assistant_agent.services.provider_http import without_unsupported_socks_proxy_env


ChatProviderName = Literal["mock", "openai", "qwen", "ark", "deepseek", "local"]
ChatStreamCallback = Callable[[str, dict[str, Any]], None]


ProviderChatCapabilities = ProviderCapabilities


def chat_capabilities_for_provider(provider: str) -> ProviderChatCapabilities:
    """Return conservative OpenAI-compatible chat capabilities for a provider."""

    spec = CHAT_PROVIDER_SPECS.get(provider.lower())
    if spec is None:
        return ProviderChatCapabilities()
    return spec.capabilities


class ChatRequest(BaseModel):
    """Input for direct chat providers."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_query: str = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None
    memory_context: list[str] = Field(default_factory=list)
    system_instruction: str | None = None
    response_format: dict[str, Any] | None = None
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1)
    stream_callback: ChatStreamCallback | None = Field(default=None, exclude=True)


class ChatProviderError(BaseModel):
    """Structured provider error returned by chat adapters."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class ChatResult(BaseModel):
    """Structured direct chat result."""

    response_text: str = ""
    tool_calls: list[NativeToolCall] = Field(default_factory=list)
    reasoning_content: str | None = Field(default=None, exclude=True)
    finish_reason: str | None = None
    refusal: str | None = None
    message_kind: str | None = None
    provider: str = Field(min_length=1)
    model: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = Field(default=None, ge=0)
    errors: list[ChatProviderError] = Field(default_factory=list)
    output_ref: str | None = None

    @property
    def success(self) -> bool:
        return not self.errors


class ChatAdapter(Protocol):
    """Provider boundary for direct chat."""

    def chat(self, request: ChatRequest) -> ChatResult:
        """Return a direct text response."""


class AsyncStreamingChatAdapter(Protocol):
    """Optional provider boundary for async LLM event streams."""

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        """Return provider-neutral streaming events without replacing chat()."""


def _mock_chat_response_text(request: ChatRequest) -> str:
    context_note = ""
    if request.memory_context:
        context_note = f" 已参考 {len(request.memory_context)} 条记忆。"
    return f"已收到你的请求：{request.user_query}。这是一个离线 mock direct_chat 回复。{context_note}".strip()


def _mock_chat_usage(request: ChatRequest) -> dict[str, int]:
    return {
        "input_chars": len(request.user_query),
        "output_chars": 35 + len(request.user_query),
    }


class MockChatAdapter:
    """Deterministic local chat adapter used by default tests and runtime."""

    provider = "mock"
    model = "mock-direct-chat"

    def chat(self, request: ChatRequest) -> ChatResult:
        response_text = _mock_chat_response_text(request)
        usage = _mock_chat_usage(request)
        _emit_stream_delta(
            request.stream_callback,
            response_text,
            provider=self.provider,
            model=self.model,
            token_streaming=False,
            chunking_strategy="mock_full_text",
        )
        return ChatResult(
            response_text=response_text,
            provider=self.provider,
            model=self.model,
            usage=usage,
            latency_ms=1,
            output_ref="mock://chat/direct",
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        response_text = _mock_chat_response_text(request)
        usage = _mock_chat_usage(request)
        yield LLMEvent(
            event_type="token_delta",
            provider=self.provider,
            model=self.model,
            text=response_text,
            metadata={
                "token_streaming": False,
                "chunking_strategy": "mock_full_text",
            },
        )
        yield LLMEvent(
            event_type="completed",
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            usage=usage,
        )


class UnconfiguredChatAdapter:
    """Adapter returned when a real chat provider is selected without config."""

    def __init__(self, provider: str, missing: str) -> None:
        self.provider = provider
        self.missing = missing

    def chat(self, request: ChatRequest) -> ChatResult:
        error = build_provider_error(
            "provider_unconfigured",
            f"{self.provider} chat provider is missing {self.missing}.",
            recoverable=True,
            provider=self.provider,
            capability="direct_chat",
        )
        return ChatResult(
            response_text="",
            provider=self.provider,
            model=None,
            errors=[
                ChatProviderError(
                    code=error.code,
                    message=error.message,
                    recoverable=error.recoverable,
                )
            ],
        )


class OpenAICompatibleChatAdapter:
    """OpenAI-compatible SDK chat adapter for explicit real provider smoke."""

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
        stream: bool = False,
        enable_thinking: bool | None = None,
        client: Any | None = None,
        async_client: Any | None = None,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.stream = stream
        self.enable_thinking = enable_thinking
        self.capabilities = chat_capabilities_for_provider(provider)
        self._client = client
        if self._client is None and async_client is None:
            with without_unsupported_socks_proxy_env():
                self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        self._async_client = async_client

    def chat(self, request: ChatRequest) -> ChatResult:
        started_at = time.perf_counter()
        payload = _build_chat_completions_payload(
            request,
            self.model,
            self.capabilities,
            stream=self.stream,
            extra_body=self._extra_body(),
        )
        try:
            if self.stream:
                stream = self._sdk_client().chat.completions.create(**payload)
                return _parse_openai_chat_stream(
                    stream,
                    provider=self.provider,
                    model=self.model,
                    latency_ms=int((time.perf_counter() - started_at) * 1000),
                    stream_callback=request.stream_callback,
                )
            data = self._sdk_client().chat.completions.create(**payload)
            return _parse_openai_chat_response(
                data,
                provider=self.provider,
                model=self.model,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )
        except (
            ProviderAdapterError,
            APITimeoutError,
            TimeoutError,
            AuthenticationError,
            PermissionDeniedError,
            RateLimitError,
            APIConnectionError,
            APIStatusError,
            OpenAIError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            return _chat_error_from_exception(self.provider, exc)

    def _sdk_client(self) -> Any:
        if self._client is None:
            with without_unsupported_socks_proxy_env():
                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                )
        return self._client

    def _async_sdk_client(self) -> Any:
        if self._async_client is None:
            from openai import AsyncOpenAI

            with without_unsupported_socks_proxy_env():
                self._async_client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                )
        return self._async_client

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[LLMEvent]:
        payload = _build_chat_completions_payload(
            request,
            self.model,
            self.capabilities,
            stream=True,
            extra_body=self._extra_body(),
        )
        stream: Any | None = None
        try:
            stream_result = self._async_sdk_client().chat.completions.create(**payload)
            stream = await stream_result if inspect.isawaitable(stream_result) else stream_result
            async for event in _openai_async_chat_stream_events(
                stream,
                provider=self.provider,
                model=self.model,
            ):
                yield event
        except (
            ProviderAdapterError,
            APITimeoutError,
            TimeoutError,
            AuthenticationError,
            PermissionDeniedError,
            RateLimitError,
            APIConnectionError,
            APIStatusError,
            OpenAIError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            yield _llm_error_event_from_exception(self.provider, self.model, exc)
        finally:
            if stream is not None:
                await _close_provider_stream(stream)

    def _extra_body(self) -> dict[str, Any] | None:
        if self.provider != "qwen" or self.enable_thinking is None:
            return None
        return {"enable_thinking": self.enable_thinking}


def create_chat_adapter(config: ProviderConfig | None = None) -> ChatAdapter:
    """Create the default chat adapter without initializing real provider clients."""

    resolved = config or ProviderConfig.from_env()
    settings = resolved.resolved_chat_provider()
    missing = settings.missing_required_env()
    if missing:
        return UnconfiguredChatAdapter(resolved.chat_provider, ", ".join(missing))
    if settings.adapter_kind == "openai_compatible":
        return OpenAICompatibleChatAdapter(
            provider=settings.provider,
            api_key=settings.api_key or "",
            base_url=settings.base_url or "",
            model=settings.model or "",
            timeout_seconds=resolved.chat_timeout_seconds,
            stream=resolved.chat_stream,
            enable_thinking=(
                resolved.qwen_chat_enable_thinking if settings.provider == "qwen" else None
            ),
        )
    return MockChatAdapter()


def _build_chat_completions_payload(
    request: ChatRequest,
    model: str,
    capabilities: ProviderChatCapabilities,
    *,
    stream: bool = False,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if request.messages:
        messages = request.messages
    else:
        messages = []
        if request.system_instruction:
            messages.append({"role": "system", "content": request.system_instruction})
        if request.memory_context:
            messages.append({"role": "system", "content": "相关记忆：\n" + "\n".join(request.memory_context)})
        messages.append({"role": "user", "content": request.user_query})
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": request.temperature,
    }
    if capabilities.max_tokens_param:
        payload[capabilities.max_tokens_param] = request.max_tokens
    if request.tools and capabilities.supports_native_tools:
        payload["tools"] = request.tools
        if capabilities.supports_tool_choice:
            payload["tool_choice"] = request.tool_choice or "auto"
    if request.response_format and capabilities.supports_response_format:
        payload["response_format"] = request.response_format
    if extra_body:
        payload["extra_body"] = dict(extra_body)
    if stream:
        payload["stream"] = True
        if capabilities.include_stream_usage:
            payload["stream_options"] = {"include_usage": True}
    return payload


def _parse_openai_chat_response(
    data: Any,
    *,
    provider: str,
    model: str,
    latency_ms: int,
) -> ChatResult:
    data = _to_plain_data(data)
    if not isinstance(data, dict):
        raise ProviderAdapterError("provider_bad_response", "chat provider returned a non-object response")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderAdapterError("provider_bad_response", "chat provider returned no choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderAdapterError("provider_bad_response", "chat provider returned an invalid choice")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderAdapterError("provider_bad_response", "chat provider returned an invalid message")
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    tool_calls = _parse_openai_tool_calls(message.get("tool_calls"))
    reasoning_content = message.get("reasoning_content")
    refusal = message.get("refusal") if isinstance(message.get("refusal"), str) else None
    if (not isinstance(content, str) or not content.strip()) and not tool_calls and not refusal:
        raise ProviderAdapterError("provider_empty_response", "chat provider returned empty content")
    usage = data.get("usage")
    finish_reason = choice.get("finish_reason")
    return ChatResult(
        response_text=content.strip() if isinstance(content, str) else "",
        tool_calls=tool_calls,
        reasoning_content=reasoning_content if isinstance(reasoning_content, str) and reasoning_content else None,
        finish_reason=str(finish_reason) if finish_reason is not None else None,
        refusal=refusal,
        message_kind=_chat_message_kind(tool_calls=tool_calls, refusal=refusal, content=content),
        provider=provider,
        model=str(data.get("model") or model),
        usage=usage if isinstance(usage, dict) else {},
        latency_ms=latency_ms,
        output_ref=f"provider://chat/{provider}",
    )


def _parse_openai_chat_stream(
    stream: Any,
    *,
    provider: str,
    model: str,
    latency_ms: int,
    stream_callback: ChatStreamCallback | None = None,
) -> ChatResult:
    accumulator = LLMEventAccumulator()
    state = _OpenAIStreamState(response_model=model)
    refusal = ""
    for chunk in stream:
        for event in _openai_chat_chunk_events(chunk, provider=provider, state=state):
            accumulator.apply(event)
            if event.event_type == "token_delta":
                _emit_stream_delta(
                    stream_callback,
                    event.text or "",
                    provider=event.provider,
                    model=event.model,
                    token_streaming=True,
                    finish_reason=event.finish_reason,
                )
    completed_event = _openai_stream_completed_event(provider=provider, state=state)
    accumulator.apply(completed_event)
    raw_refusal = completed_event.metadata.get("refusal")
    if isinstance(raw_refusal, str):
        refusal = raw_refusal

    content = accumulator.response_text
    tool_calls = accumulator.finalize_tool_calls(provider_format="openai_compatible")
    reasoning_content = "".join(state.reasoning_content_parts)
    if not content.strip() and not tool_calls and not refusal:
        raise ProviderAdapterError("provider_empty_response", "chat provider returned empty content")
    return ChatResult(
        response_text=content.strip(),
        tool_calls=tool_calls,
        reasoning_content=reasoning_content or None,
        finish_reason=accumulator.finish_reason,
        refusal=refusal or None,
        message_kind=_chat_message_kind(tool_calls=tool_calls, refusal=refusal or None, content=content),
        provider=provider,
        model=accumulator.model or model,
        usage=accumulator.usage,
        latency_ms=latency_ms,
        output_ref=f"provider://chat/{provider}",
    )


@dataclass
class _OpenAIStreamState:
    response_model: str
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    refusal_parts: list[str] = field(default_factory=list)
    reasoning_content_parts: list[str] = field(default_factory=list)


def _openai_chat_chunk_events(
    chunk: Any,
    *,
    provider: str,
    state: _OpenAIStreamState,
) -> Iterator[LLMEvent]:
    data = _to_plain_data(chunk)
    if not isinstance(data, dict):
        return
    if data.get("model"):
        state.response_model = str(data["model"])
    chunk_usage = data.get("usage")
    if isinstance(chunk_usage, dict):
        state.usage = dict(chunk_usage)
    choices = data.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason") is not None:
            state.finish_reason = str(choice["finish_reason"])
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            yield LLMEvent(
                event_type="token_delta",
                provider=provider,
                model=state.response_model,
                text=content,
                metadata={"token_streaming": True, "chunking_strategy": "provider_token_delta"},
            )
        elif isinstance(content, list):
            chunk_text = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
            if chunk_text:
                yield LLMEvent(
                    event_type="token_delta",
                    provider=provider,
                    model=state.response_model,
                    text=chunk_text,
                    metadata={"token_streaming": True, "chunking_strategy": "provider_token_delta"},
                )
        refusal = delta.get("refusal")
        if isinstance(refusal, str):
            state.refusal_parts.append(refusal)
        reasoning_content = delta.get("reasoning_content")
        if isinstance(reasoning_content, str):
            state.reasoning_content_parts.append(reasoning_content)
            yield LLMEvent(
                event_type="reasoning_delta",
                provider=provider,
                model=state.response_model,
                text=reasoning_content,
            )
        yield from _openai_tool_call_delta_events(
            delta.get("tool_calls"),
            provider=provider,
            model=state.response_model,
        )


def _openai_stream_completed_event(*, provider: str, state: _OpenAIStreamState) -> LLMEvent:
    metadata: dict[str, Any] = {}
    refusal_text = "".join(state.refusal_parts)
    if refusal_text:
        metadata["refusal"] = refusal_text
    return LLMEvent(
        event_type="completed",
        provider=provider,
        model=state.response_model,
        finish_reason=state.finish_reason,
        usage=dict(state.usage),
        metadata=metadata,
    )


def _openai_chat_stream_events(stream: Any, *, provider: str, model: str) -> Iterator[LLMEvent]:
    """Translate OpenAI-compatible stream chunks into provider-neutral events."""

    state = _OpenAIStreamState(response_model=model)
    for chunk in stream:
        yield from _openai_chat_chunk_events(chunk, provider=provider, state=state)
    yield _openai_stream_completed_event(provider=provider, state=state)


async def _openai_async_chat_stream_events(
    stream: Any,
    *,
    provider: str,
    model: str,
) -> AsyncIterator[LLMEvent]:
    """Translate OpenAI-compatible async stream chunks into provider-neutral events."""

    state = _OpenAIStreamState(response_model=model)
    async for chunk in stream:
        for event in _openai_chat_chunk_events(chunk, provider=provider, state=state):
            yield event
    yield _openai_stream_completed_event(provider=provider, state=state)


def _emit_stream_delta(
    stream_callback: ChatStreamCallback | None,
    text: str,
    *,
    provider: str,
    model: str | None,
    token_streaming: bool,
    finish_reason: str | None = None,
    chunking_strategy: str = "provider_token_delta",
) -> None:
    if stream_callback is None or not text:
        return
    payload: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "token_streaming": token_streaming,
        "chunking_strategy": chunking_strategy,
    }
    if finish_reason is not None:
        payload["finish_reason"] = finish_reason
    stream_callback(text, payload)


def _chat_message_kind(*, tool_calls: list[NativeToolCall], refusal: str | None, content: Any) -> str:
    if tool_calls:
        return "tool_call"
    if refusal:
        return "refusal"
    if isinstance(content, str) and content.strip():
        return "final_answer"
    return "empty"


def _parse_openai_tool_calls(value: Any) -> list[NativeToolCall]:
    if not isinstance(value, list):
        return []
    calls: list[NativeToolCall] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            calls.append(openai_tool_call_to_native_tool_call(item))
        except ValueError:
            continue
    return calls


def _openai_tool_call_delta_events(
    value: Any,
    *,
    provider: str,
    model: str | None,
) -> Iterator[LLMEvent]:
    if not isinstance(value, list):
        return
    for position, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            index = position
        delta_kwargs: dict[str, Any] = {"index": index}
        if item.get("id") is not None:
            delta_kwargs["id"] = str(item["id"])
        if item.get("type") is not None:
            delta_kwargs["type"] = str(item["type"])
        function = item.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                delta_kwargs["name_delta"] = name
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                delta_kwargs["arguments_delta"] = arguments
        if len(delta_kwargs) == 1:
            continue
        yield LLMEvent(
            event_type="tool_call_delta",
            provider=provider,
            model=model,
            tool_call_delta=LLMToolCallDelta(**delta_kwargs),
        )


def _to_plain_data(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: _to_plain_data(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    if hasattr(value, "model_dump"):
        return _to_plain_data(value.model_dump(mode="json"))
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {
            key: _to_plain_data(child)
            for key, child in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _chat_error(provider: str, code: str, message: object, *, recoverable: bool | None = None) -> ChatResult:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider=provider,
        capability="direct_chat",
    )
    return ChatResult(
        response_text="",
        provider=provider,
        model=None,
        errors=[
            ChatProviderError(
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
            )
        ],
    )


def _llm_error_event_from_exception(provider: str, model: str | None, exc: Exception) -> LLMEvent:
    result = _chat_error_from_exception(provider, exc)
    error = (
        result.errors[0]
        if result.errors
        else ChatProviderError(
            code="provider_unknown_error",
            message=_safe_llm_provider_error_message("provider_unknown_error"),
            recoverable=False,
        )
    )
    return LLMEvent(
        event_type="error",
        provider=provider,
        model=model,
        error=LLMProviderError(
            code=error.code,
            message=_safe_llm_provider_error_message(error.code),
            recoverable=error.recoverable,
        ),
    )


def _safe_llm_provider_error_message(code: str) -> str:
    messages = {
        "provider_auth_failed": "Chat provider authentication failed.",
        "provider_bad_response": "Chat provider returned an invalid response.",
        "provider_context_overflow": "Chat provider context limit was exceeded.",
        "provider_empty_response": "Chat provider returned empty content.",
        "provider_network_error": "Chat provider network request failed.",
        "provider_rate_limited": "Chat provider rate limit was reached.",
        "provider_timeout": "Chat provider request timed out.",
        "provider_unavailable": "Chat provider is unavailable.",
        "provider_unknown_error": "Chat provider error.",
    }
    return messages.get(code, "Chat provider error.")


async def _close_provider_stream(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if callable(aclose):
        result = aclose()
        if inspect.isawaitable(result):
            await result
        return
    close = getattr(stream, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


def _chat_error_from_exception(provider: str, exc: Exception) -> ChatResult:
    if isinstance(exc, ProviderAdapterError):
        return _chat_error(provider, exc.code, exc.message)
    if isinstance(exc, (APITimeoutError, TimeoutError)):
        return _chat_error(provider, "provider_timeout", str(exc), recoverable=True)
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return _chat_error(provider, "provider_auth_failed", str(exc))
    if isinstance(exc, RateLimitError):
        return _chat_error(provider, "provider_rate_limited", str(exc), recoverable=True)
    if isinstance(exc, APIConnectionError):
        return _chat_error(provider, "provider_network_error", str(exc), recoverable=True)
    if isinstance(exc, APIStatusError):
        return _chat_error(provider, _api_status_error_code(exc), str(exc))
    if isinstance(exc, OpenAIError):
        return _chat_error(provider, "provider_unavailable", str(exc), recoverable=True)
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return _chat_error(provider, "provider_bad_response", str(exc))
    return _chat_error(provider, "provider_unknown_error", str(exc))


def _api_status_error_code(exc: APIStatusError) -> str:
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if _looks_like_context_overflow(exc) or status_code == 413:
        return "provider_context_overflow"
    if isinstance(status_code, int):
        return _http_error_code(status_code)
    return "provider_bad_response"


def _looks_like_context_overflow(exc: APIStatusError) -> bool:
    body = getattr(exc, "body", None)
    text = f"{exc} {body}".lower()
    markers = (
        "context_length_exceeded",
        "context length",
        "maximum context",
        "max context",
        "token limit",
        "too many tokens",
        "request too large",
    )
    return any(marker in text for marker in markers)


def _http_error_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "provider_auth_failed"
    if status_code == 413:
        return "provider_context_overflow"
    if status_code == 429:
        return "provider_rate_limited"
    if 500 <= status_code:
        return "provider_unavailable"
    return "provider_bad_response"

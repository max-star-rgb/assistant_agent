"""Direct chat adapter contracts and local implementations."""

from dataclasses import dataclass
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
from assistant_agent.services.provider_errors import ProviderAdapterError, build_provider_error


ChatProviderName = Literal["mock", "openai", "qwen", "deepseek", "local"]


@dataclass(frozen=True)
class ProviderChatCapabilities:
    """OpenAI-compatible chat payload switches for one provider."""

    supports_response_format: bool = True
    supports_native_tools: bool = True
    supports_tool_choice: bool = True
    max_tokens_param: str | None = "max_tokens"
    include_stream_usage: bool = False


_OPENAI_COMPATIBLE_CHAT_CAPABILITIES = ProviderChatCapabilities()
_PROVIDER_CHAT_CAPABILITIES: dict[str, ProviderChatCapabilities] = {
    "openai": _OPENAI_COMPATIBLE_CHAT_CAPABILITIES,
    "qwen": _OPENAI_COMPATIBLE_CHAT_CAPABILITIES,
    "deepseek": _OPENAI_COMPATIBLE_CHAT_CAPABILITIES,
}


def chat_capabilities_for_provider(provider: str) -> ProviderChatCapabilities:
    """Return conservative OpenAI-compatible chat capabilities for a provider."""

    return _PROVIDER_CHAT_CAPABILITIES.get(provider.lower(), _OPENAI_COMPATIBLE_CHAT_CAPABILITIES)


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


class ChatProviderError(BaseModel):
    """Structured provider error returned by chat adapters."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class ChatResult(BaseModel):
    """Structured direct chat result."""

    response_text: str = ""
    tool_calls: list[NativeToolCall] = Field(default_factory=list)
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


class MockChatAdapter:
    """Deterministic local chat adapter used by default tests and runtime."""

    provider = "mock"
    model = "mock-direct-chat"

    def chat(self, request: ChatRequest) -> ChatResult:
        context_note = ""
        if request.memory_context:
            context_note = f" 已参考 {len(request.memory_context)} 条记忆。"
        return ChatResult(
            response_text=f"已收到你的请求：{request.user_query}。这是一个离线 mock direct_chat 回复。{context_note}".strip(),
            provider=self.provider,
            model=self.model,
            usage={
                "input_chars": len(request.user_query),
                "output_chars": 35 + len(request.user_query),
            },
            latency_ms=1,
            output_ref="mock://chat/direct",
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
        client: Any | None = None,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.stream = stream
        self.capabilities = chat_capabilities_for_provider(provider)
        self._client = (
            client
            if client is not None
            else OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        )

    def chat(self, request: ChatRequest) -> ChatResult:
        started_at = time.perf_counter()
        payload = _build_chat_completions_payload(
            request,
            self.model,
            self.capabilities,
            stream=self.stream,
        )
        try:
            if self.stream:
                stream = self._client.chat.completions.create(**payload)
                return _parse_openai_chat_stream(
                    stream,
                    provider=self.provider,
                    model=self.model,
                    latency_ms=int((time.perf_counter() - started_at) * 1000),
                )
            data = self._client.chat.completions.create(**payload)
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


def create_chat_adapter(config: ProviderConfig | None = None) -> ChatAdapter:
    """Create the default chat adapter without initializing real provider clients."""

    resolved = config or ProviderConfig.from_env()
    settings = resolved.resolved_chat_provider()
    missing = settings.missing_required_env()
    if missing:
        return UnconfiguredChatAdapter(resolved.chat_provider, ", ".join(missing))
    if settings.spec.adapter_kind == "openai_compatible":
        return OpenAICompatibleChatAdapter(
            provider=settings.provider,
            api_key=settings.api_key or "",
            base_url=settings.base_url or "",
            model=settings.model or "",
            stream=resolved.chat_stream,
        )
    return MockChatAdapter()


HttpChatAdapter = OpenAICompatibleChatAdapter


def _build_chat_completions_payload(
    request: ChatRequest,
    model: str,
    capabilities: ProviderChatCapabilities,
    *,
    stream: bool = False,
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
    refusal = message.get("refusal") if isinstance(message.get("refusal"), str) else None
    if (not isinstance(content, str) or not content.strip()) and not tool_calls and not refusal:
        raise ProviderAdapterError("provider_empty_response", "chat provider returned empty content")
    usage = data.get("usage")
    finish_reason = choice.get("finish_reason")
    return ChatResult(
        response_text=content.strip() if isinstance(content, str) else "",
        tool_calls=tool_calls,
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
) -> ChatResult:
    content_parts: list[str] = []
    refusal_parts: list[str] = []
    tool_call_deltas: dict[int, dict[str, Any]] = {}
    response_model = model
    finish_reason: str | None = None
    usage: dict[str, Any] = {}

    for chunk in stream:
        data = _to_plain_data(chunk)
        if not isinstance(data, dict):
            continue
        if data.get("model"):
            response_model = str(data["model"])
        chunk_usage = data.get("usage")
        if isinstance(chunk_usage, dict):
            usage = chunk_usage
        choices = data.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason") is not None:
                finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str):
                content_parts.append(content)
            elif isinstance(content, list):
                content_parts.append(
                    "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
                )
            refusal = delta.get("refusal")
            if isinstance(refusal, str):
                refusal_parts.append(refusal)
            _merge_stream_tool_call_deltas(tool_call_deltas, delta.get("tool_calls"))

    message: dict[str, Any] = {
        "content": "".join(content_parts),
    }
    refusal = "".join(refusal_parts)
    if refusal:
        message["refusal"] = refusal
    tool_calls = [_finalize_stream_tool_call(value) for _, value in sorted(tool_call_deltas.items())]
    if tool_calls:
        message["tool_calls"] = tool_calls
    return _parse_openai_chat_response(
        {
            "model": response_model,
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage,
        },
        provider=provider,
        model=model,
        latency_ms=latency_ms,
    )


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


def _merge_stream_tool_call_deltas(tool_call_deltas: dict[int, dict[str, Any]], value: Any) -> None:
    if not isinstance(value, list):
        return
    for position, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            index = position
        current = tool_call_deltas.setdefault(
            index,
            {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if item.get("id") is not None:
            current["id"] = str(item["id"])
        if item.get("type") is not None:
            current["type"] = str(item["type"])
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        current_function = current.setdefault("function", {"name": "", "arguments": ""})
        name = function.get("name")
        if isinstance(name, str) and name:
            existing_name = current_function.get("name")
            if not isinstance(existing_name, str) or not existing_name:
                current_function["name"] = name
            elif name == existing_name:
                current_function["name"] = existing_name
            elif name.startswith(existing_name):
                current_function["name"] = name
            else:
                current_function["name"] = existing_name + name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            current_function["arguments"] = str(current_function.get("arguments") or "") + arguments


def _finalize_stream_tool_call(value: dict[str, Any]) -> dict[str, Any]:
    function = value.get("function") if isinstance(value.get("function"), dict) else {}
    return {
        "id": value.get("id"),
        "type": value.get("type") or "function",
        "function": {
            "name": function.get("name") or "",
            "arguments": function.get("arguments") or "",
        },
    }


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

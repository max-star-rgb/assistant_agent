"""Direct chat adapter contracts and local implementations."""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.assistant_decision import NativeToolCall, openai_tool_call_to_native_tool_call
from multimodal_agent.services.provider_errors import ProviderAdapterError, build_provider_error


ChatProviderName = Literal["mock", "openai", "qwen", "deepseek", "local"]


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


class HttpChatAdapter:
    """OpenAI-compatible HTTP chat adapter for explicit real provider smoke."""

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout_seconds = timeout_seconds

    def chat(self, request: ChatRequest) -> ChatResult:
        started_at = time.perf_counter()
        payload = _build_openai_chat_payload(request, self.model)
        http_request = urllib.request.Request(
            _chat_completions_url(self.base_url),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            return _parse_openai_chat_response(
                data,
                provider=self.provider,
                model=self.model,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )
        except TimeoutError as exc:
            return _chat_error(self.provider, "provider_timeout", str(exc), recoverable=True)
        except urllib.error.HTTPError as exc:
            return _chat_error(self.provider, _http_error_code(exc.code), f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            return _chat_error(self.provider, "provider_unavailable", str(exc.reason), recoverable=True)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, ProviderAdapterError) as exc:
            code = exc.code if isinstance(exc, ProviderAdapterError) else "provider_bad_response"
            return _chat_error(self.provider, code, str(exc))


def create_chat_adapter(config: ProviderConfig | None = None) -> ChatAdapter:
    """Create the default chat adapter without initializing real provider clients."""

    resolved = config or ProviderConfig.from_env()
    settings = resolved.resolved_chat_provider()
    missing = settings.missing_required_env()
    if missing:
        return UnconfiguredChatAdapter(resolved.chat_provider, ", ".join(missing))
    if settings.spec.adapter_kind == "openai_compatible":
        return HttpChatAdapter(
            provider=settings.provider,
            api_key=settings.api_key or "",
            base_url=settings.base_url or "",
            model=settings.model or "",
        )
    return MockChatAdapter()


def _build_openai_chat_payload(request: ChatRequest, model: str) -> dict[str, Any]:
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
        "max_tokens": request.max_tokens,
    }
    if request.tools:
        payload["tools"] = request.tools
        payload["tool_choice"] = request.tool_choice or "auto"
    if request.response_format:
        payload["response_format"] = request.response_format
    return payload


def _parse_openai_chat_response(
    data: dict[str, Any],
    *,
    provider: str,
    model: str,
    latency_ms: int,
) -> ChatResult:
    choice = data["choices"][0]
    message = choice["message"]
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


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


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

"""Direct chat adapter contracts and local implementations."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.provider_errors import build_provider_error


ChatProviderName = Literal["mock", "openai", "qwen", "local"]


class ChatRequest(BaseModel):
    """Input for direct chat providers."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_query: str = Field(min_length=1)
    memory_context: list[str] = Field(default_factory=list)
    system_instruction: str | None = None
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


def create_chat_adapter(config: ProviderConfig | None = None) -> ChatAdapter:
    """Create the default chat adapter without initializing real provider clients."""

    resolved = config or ProviderConfig.from_env()
    if resolved.chat_provider == "openai" and not resolved.openai_api_key:
        return UnconfiguredChatAdapter("openai", "OPENAI_API_KEY")
    if resolved.chat_provider == "qwen" and not resolved.qwen_api_key:
        return UnconfiguredChatAdapter("qwen", "QWEN_API_KEY")
    if resolved.chat_provider == "local" and not resolved.local_chat_base_url:
        return UnconfiguredChatAdapter("local", "LOCAL_CHAT_BASE_URL")
    return MockChatAdapter()

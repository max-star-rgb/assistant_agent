"""LangChain-native chat model composition for supported providers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
)
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
)
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from assistant_agent.config import ChatConfig
from assistant_agent.provider_mode import ProviderMode
from assistant_agent.providers.provider_http import without_unsupported_socks_proxy_env


class ProviderConfigurationError(RuntimeError):
    """The selected native chat provider is incomplete or unsupported."""


class MockAssistantChatModel(BaseChatModel):
    """Deterministic offline model with standard LangChain message contracts."""

    model_name: str = "mock-native-chat"

    @property
    def _llm_type(self) -> str:
        return "assistant-agent-mock"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name, "provider": "mock"}

    def _generate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager
        message = self._response_message(messages, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del stop, run_manager
        message = self._response_message(messages, **kwargs)
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=message.content,
                response_metadata=message.response_metadata,
                usage_metadata=message.usage_metadata,
            )
        )

    async def _astream(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._stream(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            yield chunk

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        """Expose schemas through the same Runnable contract create_agent expects."""

        normalized = [convert_to_openai_tool(tool) for tool in tools]
        return self.bind(tools=normalized, tool_choice=tool_choice, **kwargs)

    def _response_message(self, messages: list[AnyMessage], **kwargs: Any) -> AIMessage:
        structured = _mock_structured_tool_call(kwargs.get("tools"), messages)
        if structured is not None and kwargs.get("tool_choice") == "any":
            name, arguments = structured
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": arguments,
                        "id": f"mock-structured-{name}",
                        "type": "tool_call",
                    }
                ],
            )
        query = _last_human_text(messages)
        input_tokens = len(query)
        output_tokens = 12
        return AIMessage(
            content=f"已收到：{query}",
            response_metadata={
                "model_name": self.model_name,
                "provider": "mock",
            },
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        )


def create_chat_model(
    config: ChatConfig,
    *,
    provider_mode: ProviderMode,
) -> BaseChatModel:
    """Create one standard chat model without silently changing provider mode."""

    settings = config.resolved_provider()
    if provider_mode == "mock":
        return MockAssistantChatModel()

    missing = settings.missing_required_env()
    if missing:
        raise ProviderConfigurationError(
            f"{settings.provider} chat provider is missing {', '.join(missing)}"
        )
    if settings.provider == "qwen" and config.qwen_chat_api_protocol == "dashscope":
        from assistant_agent.providers.dashscope_langchain import (
            DashScopeNativeChatModel,
        )

        return DashScopeNativeChatModel(
            api_key=settings.api_key or "",
            base_url=settings.base_url or "",
            model_name=settings.model or "",
            output_version="v1",
            timeout_seconds=config.chat_timeout_seconds,
            enable_thinking=config.qwen_chat_enable_thinking,
            enable_search=config.qwen_chat_enable_search,
            streaming=(config.native_provider_streaming or config.chat_stream),
        )
    if settings.adapter_kind not in {"openai_compatible", "local_http"}:
        raise ProviderConfigurationError(
            f"unsupported native chat adapter: {settings.adapter_kind}"
        )

    from langchain_openai import ChatOpenAI

    extra_body = _provider_extra_body(config, settings.provider)
    streaming = config.native_provider_streaming or config.chat_stream
    with without_unsupported_socks_proxy_env():
        return ChatOpenAI(
            api_key=settings.api_key or "not-required",
            base_url=settings.base_url,
            model=settings.model or "",
            timeout=config.chat_timeout_seconds,
            streaming=streaming,
            stream_usage=streaming,
            extra_body=extra_body,
            metadata={"provider": settings.provider},
            max_retries=0,
        )


def _provider_extra_body(config: ChatConfig, provider: str) -> dict[str, Any] | None:
    if provider != "qwen":
        return None
    extra_body: dict[str, Any] = {
        "enable_thinking": config.qwen_chat_enable_thinking,
        "enable_search": config.qwen_chat_enable_search,
    }
    if config.qwen_chat_enable_search:
        extra_body["search_options"] = {
            "search_strategy": "turbo",
            "forced_search": False,
            "enable_search_extension": True,
            "freshness": 7,
        }
    return extra_body


def _last_human_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            if isinstance(message.content, str):
                return message.content
            return " ".join(
                str(block.get("text", ""))
                for block in message.content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return ""


def _mock_structured_tool_call(
    tools: Any,
    messages: Sequence[AnyMessage],
) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(tools, list):
        return None
    for item in tools:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if name == "CodingAnalysisResponse":
            return name, {
                "status": "succeeded",
                "findings": [],
                "covered_paths": [],
            }
    return None


__all__ = [
    "MockAssistantChatModel",
    "ProviderConfigurationError",
    "create_chat_model",
]

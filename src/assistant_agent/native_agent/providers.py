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

from assistant_agent.config import ProviderConfig
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
        structured = _mock_structured_tool_call(kwargs.get("tools"))
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


def create_chat_model(config: ProviderConfig | None = None) -> BaseChatModel:
    """Create one standard chat model without silently changing provider mode."""

    resolved = config or ProviderConfig.from_env()
    settings = resolved.resolved_chat_provider()
    if resolved.provider_mode == "mock":
        return MockAssistantChatModel()

    missing = settings.missing_required_env()
    if missing:
        raise ProviderConfigurationError(
            f"{settings.provider} chat provider is missing {', '.join(missing)}"
        )
    if settings.adapter_kind not in {"openai_compatible", "local_http"}:
        raise ProviderConfigurationError(
            f"unsupported native chat adapter: {settings.adapter_kind}"
        )

    from langchain_openai import ChatOpenAI

    extra_body = _provider_extra_body(resolved, settings.provider)
    with without_unsupported_socks_proxy_env():
        return ChatOpenAI(
            api_key=settings.api_key or "not-required",
            base_url=settings.base_url,
            model=settings.model or "",
            timeout=resolved.chat_timeout_seconds,
            streaming=resolved.native_provider_streaming or resolved.chat_stream,
            max_tokens=resolved.chat_max_tokens,
            extra_body=extra_body,
            metadata={"provider": settings.provider},
            max_retries=0,
        )


def _provider_extra_body(
    config: ProviderConfig,
    provider: str,
) -> dict[str, Any] | None:
    if provider != "qwen":
        return None
    return {
        "enable_thinking": config.qwen_chat_enable_thinking,
        "enable_search": True,
        "search_options": {
            "search_strategy": "turbo",
            "forced_search": False,
            "enable_search_extension": True,
            "enable_source": True,
            "enable_citation": True,
            "citation_format": "[<number>]",
            "freshness": 7,
        },
    }


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


def _mock_structured_tool_call(tools: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(tools, list) or not tools or not isinstance(tools[0], dict):
        return None
    function = tools[0].get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if name == "NativePlanProposal":
        return name, {
            "schema_version": "native_plan_v1",
            "nodes": [
                {
                    "node_id": "answer",
                    "display_title": "完成用户目标",
                    "objective": "完成用户目标并给出可靠答案",
                    "depends_on": [],
                    "acceptance_contract": {
                        "schema_version": "native_step_acceptance_v1",
                        "output": {
                            "artifact_type": "text",
                            "description": "最终文本答案",
                        },
                        "criteria": [
                            {
                                "criterion_id": "answer_complete",
                                "statement": "答案完整回应用户目标",
                            }
                        ],
                    },
                }
            ],
            "deliverable_bindings": [
                {"deliverable": "answer", "producer_node_id": "answer"}
            ],
            "constraint_bindings": [],
        }
    if name == "VerificationResult":
        return name, {
            "status": "passed",
            "repair_work_item_ids": [],
            "reason": "mock verification passed",
        }
    return None


__all__ = [
    "MockAssistantChatModel",
    "ProviderConfigurationError",
    "create_chat_model",
]

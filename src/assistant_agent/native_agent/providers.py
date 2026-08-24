"""LangChain-native chat model composition for supported providers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    SystemMessage,
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
        supervisor = _mock_planning_supervisor_response(messages, kwargs.get("tools"))
        if supervisor is not None:
            return supervisor
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
    if settings.provider == "qwen" and resolved.qwen_chat_api_protocol == "dashscope":
        from assistant_agent.providers.dashscope_langchain import (
            DashScopeNativeChatModel,
        )

        return DashScopeNativeChatModel(
            api_key=settings.api_key or "",
            base_url=settings.base_url or "",
            model_name=settings.model or "",
            output_version="v1",
            timeout_seconds=resolved.chat_timeout_seconds,
            max_tokens=resolved.chat_max_tokens,
            enable_thinking=resolved.qwen_chat_enable_thinking,
            enable_search=resolved.qwen_chat_enable_search,
            streaming=(resolved.native_provider_streaming or resolved.chat_stream),
        )
    if settings.adapter_kind not in {"openai_compatible", "local_http"}:
        raise ProviderConfigurationError(
            f"unsupported native chat adapter: {settings.adapter_kind}"
        )

    from langchain_openai import ChatOpenAI

    extra_body = _provider_extra_body(resolved, settings.provider)
    streaming = resolved.native_provider_streaming or resolved.chat_stream
    with without_unsupported_socks_proxy_env():
        return ChatOpenAI(
            api_key=settings.api_key or "not-required",
            base_url=settings.base_url,
            model=settings.model or "",
            timeout=resolved.chat_timeout_seconds,
            streaming=streaming,
            stream_usage=streaming,
            max_tokens=resolved.chat_max_tokens,
            extra_body=extra_body,
            metadata={"provider": settings.provider},
            max_retries=0,
        )


def coding_analysis_model_view(model: BaseChatModel) -> BaseChatModel:
    """Return a read-only analysis view with provider-native search disabled."""

    llm_type = getattr(model, "_llm_type", "")
    if llm_type == "assistant-agent-dashscope-native":
        return model.model_copy(update={"enable_search": False})
    metadata = getattr(model, "metadata", None)
    provider = metadata.get("provider") if isinstance(metadata, Mapping) else None
    extra_body = getattr(model, "extra_body", None)
    if llm_type == "openai-chat" and provider == "qwen" and isinstance(
        extra_body, Mapping
    ):
        filtered = {
            key: value
            for key, value in extra_body.items()
            if key not in {"enable_search", "search_options"}
        }
        return model.model_copy(update={"extra_body": filtered or None})
    return model


def planning_supervisor_model_view(model: BaseChatModel) -> BaseChatModel:
    """Return a Supervisor view with provider-native search disabled."""

    return coding_analysis_model_view(model)


def coding_analysis_model_settings(model: BaseChatModel) -> dict[str, Any]:
    """Return protocol-native request settings for the fixed offline profile."""

    if getattr(model, "_llm_type", "") == "assistant-agent-dashscope-native":
        return {"provider_search_profile": "none"}
    return {}


def _provider_extra_body(
    config: ProviderConfig,
    provider: str,
) -> dict[str, Any] | None:
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
        if name == "WorkerResult":
            todo_id = "answer"
            try:
                payload = json.loads(_last_human_text(list(messages)))
                if isinstance(payload, Mapping) and isinstance(payload.get("todo_id"), str):
                    todo_id = payload["todo_id"]
            except (TypeError, ValueError):
                pass
            return name, {
                "todo_id": todo_id,
                "status": "succeeded",
                "summary": "mock worker completion",
            }
    return None


def _mock_planning_supervisor_response(
    messages: Sequence[AnyMessage],
    tools: Any,
) -> AIMessage | None:
    names = _mock_tool_names(tools)
    if not {"write_todos", "task"} <= names or "WorkerResult" in names:
        return None
    marker = "当前 planning working memory（只读 JSON）：\n"
    state: dict[str, Any] = {"todos": [], "worker_results": {}}
    for message in messages:
        if not isinstance(message, SystemMessage) or not isinstance(message.content, str):
            continue
        if marker not in message.content:
            continue
        try:
            candidate = json.loads(message.content.split(marker, 1)[1])
        except (TypeError, ValueError):
            continue
        if isinstance(candidate, dict):
            state = candidate
        break
    todos = state.get("todos") if isinstance(state.get("todos"), list) else []
    results = (
        state.get("worker_results")
        if isinstance(state.get("worker_results"), Mapping)
        else {}
    )
    if not todos:
        content = _last_human_text(list(messages)).strip() or "mock planning task"
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_todos",
                    "args": {
                        "todos": [
                            {
                                "todo_id": "answer",
                                "content": content[:4_000],
                                "status": "pending",
                            }
                        ]
                    },
                    "id": "mock-write-todos",
                    "type": "tool_call",
                }
            ],
        )
    pending_ids = [
        item.get("todo_id")
        for item in todos
        if isinstance(item, Mapping)
        and item.get("status") == "pending"
        and isinstance(item.get("todo_id"), str)
        and item.get("todo_id") not in results
    ]
    if pending_ids:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"todo_id": todo_id},
                    "id": f"mock-task-{todo_id}",
                    "type": "tool_call",
                }
                for todo_id in pending_ids
            ],
        )
    summaries = [
        str(result.get("summary"))
        for result in results.values()
        if isinstance(result, Mapping) and result.get("summary")
    ]
    return AIMessage(
        content=f"已完成 planning mock：{'；'.join(summaries) or '无可执行结果'}"
    )


def _mock_tool_names(tools: Any) -> set[str]:
    if not isinstance(tools, list):
        return set()
    return {
        name
        for item in tools
        if isinstance(item, Mapping)
        and isinstance((function := item.get("function")), Mapping)
        and isinstance((name := function.get("name")), str)
    }


__all__ = [
    "MockAssistantChatModel",
    "ProviderConfigurationError",
    "coding_analysis_model_settings",
    "coding_analysis_model_view",
    "create_chat_model",
    "planning_supervisor_model_view",
]

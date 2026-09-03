"""LangChain-native model for the official DashScope Generation API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
import json
from typing import Any, Literal
from urllib.parse import urlsplit

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    ChatMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import InputTokenDetails, UsageMetadata
from langchain_core.messages.tool import (
    invalid_tool_call,
    tool_call,
    tool_call_chunk,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field, SecretStr

from assistant_agent.native_agent.search_profiles import (
    SearchProfilePolicy,
    resolve_search_profile,
)
from assistant_agent.providers.dashscope_chat import (
    UrllibDashScopeTransport,
    dashscope_generation_url,
    dashscope_multimodal_generation_url,
)


_MAX_SEARCH_SOURCES = 20
_STREAM_END = object()


def _search_options_from_policy(policy: SearchProfilePolicy) -> dict[str, Any]:
    """Emit DashScope search options only from an admitted trusted profile."""
    options: dict[str, Any] = {
        "search_strategy": policy.search_strategy or "turbo",
        "forced_search": policy.forced_search,
        "enable_search_extension": True,
        "enable_source": True,
        "enable_citation": True,
        "citation_format": "[<number>]",
    }
    if policy.assigned_site_list:
        options["assigned_site_list"] = list(policy.assigned_site_list)
    if policy.prompt_intervene:
        options["intention_options"] = {"prompt_intervene": policy.prompt_intervene}
    return options


class DashScopeProviderError(RuntimeError):
    """A sanitized failure at the DashScope provider boundary."""


class DashScopeNativeChatModel(BaseChatModel):
    """Official DashScope Generation APIs exposed as a BaseChatModel."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    api_key: SecretStr
    base_url: str
    model_name: str
    timeout_seconds: float = 75.0
    api_mode: Literal["text", "multimodal"] = "text"
    temperature: float | None = None
    enable_thinking: bool = False
    enable_search: bool = False
    streaming: bool = False
    http_transport: Any = Field(
        default_factory=UrllibDashScopeTransport,
        exclude=True,
    )

    @property
    def _llm_type(self) -> str:
        return "assistant-agent-dashscope-native"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "provider": "qwen",
            "api_protocol": "dashscope",
        }

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any | BaseTool],
        *,
        tool_choice: str | dict[str, Any] | bool | None = None,
        **kwargs: Any,
    ) -> Runnable:
        normalized = [convert_to_openai_tool(tool) for tool in tools]
        return self.bind(
            tools=normalized,
            tool_choice=_normalize_tool_choice(tool_choice),
            **kwargs,
        )

    def _generate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del run_manager
        payload = self._build_payload(messages, stop=stop, stream=False, **kwargs)
        try:
            data = self.http_transport.post_json(
                url=self._generation_url(),
                headers=self._headers(stream=False),
                payload=payload,
                timeout_seconds=self.timeout_seconds,
            )
            message = self._parse_response(data)
        except DashScopeProviderError:
            raise
        except Exception as exc:
            raise DashScopeProviderError(
                f"DashScope request failed ({type(exc).__name__})."
            ) from exc
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await asyncio.to_thread(
            self._generate,
            messages,
            stop,
            run_manager,
            **kwargs,
        )

    def _stream(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del run_manager
        if self.api_mode == "multimodal":
            raise DashScopeProviderError(
                "DashScope multimodal streaming is not supported."
            )
        payload = self._build_payload(messages, stop=stop, stream=True, **kwargs)
        stream: Iterator[dict[str, Any]] | None = None
        sources: list[dict[str, Any]] = []
        terminal_seen = False
        try:
            stream = self.http_transport.stream_sse(
                url=self._generation_url(),
                headers=self._headers(stream=True),
                payload=payload,
                timeout_seconds=self.timeout_seconds,
            )
            for data in stream:
                output, choice, raw_message = _response_parts(data)
                parsed_sources = _parse_search_sources(
                    output.get("search_info", data.get("search_info"))
                )
                if parsed_sources:
                    sources = parsed_sources
                finish_reason = _optional_text(choice.get("finish_reason"))
                terminal = finish_reason is not None
                usage = _usage_metadata(data.get("usage")) if terminal else None
                metadata = (
                    self._response_metadata(
                        data,
                        finish_reason=finish_reason,
                        sources=sources,
                    )
                    if terminal
                    else {}
                )
                chunks = _tool_call_chunks(raw_message.get("tool_calls"))
                content = _message_text(raw_message.get("content"))
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=content,
                        tool_call_chunks=chunks,
                        response_metadata=metadata,
                        usage_metadata=usage,
                    )
                )
                if finish_reason is not None:
                    terminal_seen = True
            if not terminal_seen:
                raise DashScopeProviderError(
                    "DashScope stream ended without finish_reason."
                )
        except DashScopeProviderError:
            raise
        except Exception as exc:
            raise DashScopeProviderError(
                f"DashScope stream failed ({type(exc).__name__})."
            ) from exc
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    async def _astream(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        iterator = self._stream(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )
        try:
            while True:
                item = await asyncio.to_thread(_next_item, iterator)
                if item is _STREAM_END:
                    break
                yield item
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

    async def aclose(self) -> None:
        close = getattr(self.http_transport, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    def _build_payload(
        self,
        messages: list[AnyMessage],
        *,
        stop: list[str] | None,
        stream: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        profile = kwargs.get("provider_search_profile")
        deep_research = profile == "deep_research"
        policy = (
            resolve_search_profile(
                profile, protocol="dashscope", model_name=self.model_name
            )
            if isinstance(profile, str) and not deep_research
            else None
        )
        parameters: dict[str, Any] = {
            "result_format": "message",
            "enable_thinking": self.enable_thinking or deep_research,
        }
        temperature = kwargs.get("temperature", self.temperature)
        if temperature is not None:
            if (
                isinstance(temperature, bool)
                or not isinstance(temperature, (int, float))
                or not 0 <= temperature < 2
            ):
                raise ValueError("DashScope temperature must be within [0, 2)")
            parameters["temperature"] = temperature
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is not None:
            if (
                isinstance(max_tokens, bool)
                or not isinstance(max_tokens, int)
                or max_tokens <= 0
            ):
                raise ValueError("DashScope max_tokens must be a positive integer")
            parameters["max_tokens"] = max_tokens
        response_format = kwargs.get("response_format")
        if response_format is not None:
            if response_format != {"type": "json_object"}:
                raise ValueError("DashScope response_format must request json_object")
            parameters["response_format"] = response_format
        if stream:
            parameters["incremental_output"] = True
        if stop:
            parameters["stop"] = stop

        if policy is not None:
            if policy.enable_search:
                parameters["enable_search"] = True
                parameters["search_options"] = _search_options_from_policy(policy)
        elif deep_research:
            parameters["enable_search"] = True
            parameters["search_options"] = {
                "search_strategy": "max",
                "forced_search": True,
                "enable_search_extension": True,
                "enable_source": True,
                "enable_citation": True,
                "citation_format": "[<number>]",
            }
        elif self.enable_search:
            parameters["enable_search"] = True
            parameters["search_options"] = {
                "search_strategy": "turbo",
                "forced_search": False,
                "enable_search_extension": True,
                "enable_source": True,
                "enable_citation": True,
                "citation_format": "[<number>]",
                "freshness": 7,
            }

        tools = kwargs.get("tools")
        if isinstance(tools, list):
            parameters["tools"] = tools
            tool_choice = kwargs.get("tool_choice")
            if tool_choice is not None:
                parameters["tool_choice"] = tool_choice
        return {
            "model": self.model_name,
            "input": {
                "messages": [
                    _message_to_dashscope(
                        item,
                        multimodal=self.api_mode == "multimodal",
                    )
                    for item in messages
                ]
            },
            "parameters": parameters,
        }

    def _generation_url(self) -> str:
        if self.api_mode == "multimodal":
            return dashscope_multimodal_generation_url(self.base_url)
        return dashscope_generation_url(self.base_url)

    def _parse_response(self, data: dict[str, Any]) -> AIMessage:
        output, choice, raw_message = _response_parts(data)
        raw_tool_calls = raw_message.get("tool_calls")
        parsed_calls, invalid_calls = _parse_tool_calls(raw_tool_calls)
        content = _message_text(raw_message.get("content"))
        finish_reason = _optional_text(choice.get("finish_reason"))
        sources = _parse_search_sources(
            output.get("search_info", data.get("search_info"))
        )
        if self.api_mode == "multimodal":
            structured_output = _json_object_from_text(content)
            summary = structured_output.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise DashScopeProviderError(
                    "DashScope multimodal response missing summary."
                )
            return AIMessage(
                content=summary.strip(),
                additional_kwargs={"structured_output": structured_output},
                response_metadata=self._response_metadata(
                    data,
                    finish_reason=finish_reason,
                    sources=sources,
                ),
                usage_metadata=_usage_metadata(data.get("usage")),
            )
        if not content and not parsed_calls and not invalid_calls:
            raise DashScopeProviderError("DashScope response was empty.")
        return AIMessage(
            content=content,
            tool_calls=parsed_calls,
            invalid_tool_calls=invalid_calls,
            response_metadata=self._response_metadata(
                data,
                finish_reason=finish_reason,
                sources=sources,
            ),
            usage_metadata=_usage_metadata(data.get("usage")),
        )

    def _response_metadata(
        self,
        data: Mapping[str, Any],
        *,
        finish_reason: str | None,
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "provider": "qwen",
            "api_protocol": "dashscope",
            "finish_reason": finish_reason,
            "provider_request_id": _optional_text(data.get("request_id")),
            "provider_search_sources": sources,
        }

    def _headers(self, *, stream: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        if stream:
            headers.update(
                {
                    "Accept": "text/event-stream",
                    "X-DashScope-SSE": "enable",
                }
            )
        return headers


def _message_to_dashscope(
    message: AnyMessage,
    *,
    multimodal: bool = False,
) -> dict[str, Any]:
    if isinstance(message, HumanMessage) and multimodal:
        if not isinstance(message.content, (list, tuple)):
            raise ValueError("DashScope multimodal human content must be a list.")
        return {
            "role": "user",
            "content": [
                _content_block_to_dashscope(block) for block in message.content
            ],
        }
    content = _message_text(message.content)
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": content}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": content}
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "content": content,
            "tool_call_id": message.tool_call_id,
        }
    if isinstance(message, AIMessage):
        result: dict[str, Any] = {"role": "assistant", "content": content}
        if message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.get("id"),
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(
                            call.get("args", {}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        return result
    if isinstance(message, ChatMessage):
        return {"role": message.role, "content": content}
    raise TypeError(f"unsupported DashScope message type: {type(message).__name__}")


def _content_block_to_dashscope(block: Any) -> dict[str, str]:
    if not isinstance(block, Mapping):
        raise ValueError("DashScope multimodal content block must be an object.")
    if block.get("type") == "image":
        base64 = block.get("base64")
        mime_type = block.get("mime_type")
        if (
            not isinstance(base64, str)
            or not base64.strip()
            or not isinstance(mime_type, str)
            or not mime_type.strip()
        ):
            raise ValueError(
                "DashScope multimodal image block requires base64 and mime_type."
            )
        return {"image": f"data:{mime_type};base64,{base64}"}
    if block.get("type") == "image_url":
        image_url = block.get("image_url")
        url = image_url.get("url") if isinstance(image_url, Mapping) else None
        normalized = url.strip() if isinstance(url, str) else ""
        parsed = urlsplit(normalized)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return {"image": normalized}
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        return {"text": block["text"]}
    raise ValueError("unsupported DashScope multimodal content block.")


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, (list, tuple)):
        return "" if value is None else str(value)
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and block.get("type") in {
            "text",
            "output_text",
        }:
            parts.append(str(block.get("text", "")))
        elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(part for part in parts if part)


def _json_object_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response JSON is not an object")
    return parsed


def _response_parts(
    data: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    output = data.get("output")
    if not isinstance(output, Mapping):
        raise DashScopeProviderError("DashScope response missing output.")
    choices = output.get("choices")
    if (
        not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], Mapping)
    ):
        raise DashScopeProviderError("DashScope response missing choices.")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise DashScopeProviderError("DashScope response missing message.")
    return output, choice, message


def _parse_tool_calls(value: Any) -> tuple[list[Any], list[Any]]:
    parsed: list[Any] = []
    invalid: list[Any] = []
    if not isinstance(value, list):
        return parsed, invalid
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        function = raw.get("function")
        function = function if isinstance(function, Mapping) else {}
        name = _optional_text(function.get("name") or raw.get("name"))
        arguments = function.get("arguments", raw.get("arguments", ""))
        arguments_text = (
            arguments if isinstance(arguments, str) else json.dumps(arguments)
        )
        call_id = _optional_text(raw.get("id"))
        try:
            args = json.loads(arguments_text or "{}")
            if not name or not isinstance(args, dict):
                raise ValueError("tool call requires a name and object arguments")
            parsed.append(tool_call(name=name, args=args, id=call_id))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid.append(
                invalid_tool_call(
                    name=name,
                    args=arguments_text,
                    id=call_id,
                    error=str(exc),
                )
            )
    return parsed, invalid


def _tool_call_chunks(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    chunks = []
    for position, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            continue
        function = raw.get("function")
        function = function if isinstance(function, Mapping) else {}
        arguments = function.get("arguments", raw.get("arguments"))
        raw_index = raw.get("index", position)
        provider_index = (
            raw_index
            if isinstance(raw_index, int)
            and not isinstance(raw_index, bool)
            and raw_index >= 0
            else position
        )
        # v1 content block indexes are message-global; index 0 is reserved for text.
        chunks.append(
            tool_call_chunk(
                name=_optional_text(function.get("name") or raw.get("name")),
                args=arguments if isinstance(arguments, str) else None,
                id=_optional_text(raw.get("id")),
                index=provider_index + 1,
            )
        )
    return chunks


def _parse_search_sources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    results = value.get("search_results")
    if not isinstance(results, list):
        return []
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, item in enumerate(results, start=1):
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        title = item.get("title")
        if not isinstance(url, str) or not isinstance(title, str):
            continue
        normalized_url = url.strip()[:2048]
        normalized_title = title.strip()[:300]
        parsed_url = urlsplit(normalized_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or not normalized_title
            or normalized_url in seen
        ):
            continue
        raw_index = item.get("index", position)
        index = raw_index if isinstance(raw_index, int) and raw_index >= 1 else position
        sources.append(
            {"index": index, "title": normalized_title, "url": normalized_url}
        )
        seen.add(normalized_url)
        if len(sources) >= _MAX_SEARCH_SOURCES:
            break
    return sources


def _usage_metadata(value: Any) -> UsageMetadata | None:
    if not isinstance(value, Mapping):
        return None
    input_tokens = _non_negative_int(
        value.get("input_tokens", value.get("prompt_tokens", 0))
    )
    output_tokens = _non_negative_int(
        value.get("output_tokens", value.get("completion_tokens", 0))
    )
    total_tokens = _non_negative_int(
        value.get("total_tokens", input_tokens + output_tokens)
    )
    usage = UsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )
    raw_details = value.get("prompt_tokens_details")
    if not isinstance(raw_details, Mapping):
        raw_details = value.get("input_tokens_details")
    cache_read = (
        raw_details.get("cached_tokens")
        if isinstance(raw_details, Mapping)
        else None
    )
    if cache_read is None:
        cache_read = value.get("cached_tokens")
    cache_creation = (
        raw_details.get("cache_creation_input_tokens")
        if isinstance(raw_details, Mapping)
        else None
    )
    details = InputTokenDetails()
    if (
        isinstance(cache_read, int)
        and not isinstance(cache_read, bool)
        and cache_read >= 0
    ):
        details["cache_read"] = cache_read
    if (
        isinstance(cache_creation, int)
        and not isinstance(cache_creation, bool)
        and cache_creation >= 0
    ):
        details["cache_creation"] = cache_creation
    if details:
        usage["input_token_details"] = details
    return usage


def _non_negative_int(value: Any) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.lower() != "null" else None


def _normalize_tool_choice(value: Any) -> Any:
    if value is True or value == "any":
        return "required"
    if value is False:
        return "none"
    if isinstance(value, str) and value not in {"auto", "none", "required"}:
        return {"type": "function", "function": {"name": value}}
    return value


def _next_item(iterator: Iterator[Any]) -> Any:
    try:
        return next(iterator)
    except StopIteration:
        return _STREAM_END


__all__ = ["DashScopeNativeChatModel", "DashScopeProviderError"]

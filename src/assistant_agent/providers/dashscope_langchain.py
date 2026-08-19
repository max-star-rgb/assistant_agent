"""LangChain-native model for the official DashScope Generation API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
import json
import re
from typing import Any
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
from langchain_core.messages.tool import (
    invalid_tool_call,
    tool_call,
    tool_call_chunk,
)
from langchain_core.messages.content import create_citation, create_text_block
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field, SecretStr

from assistant_agent.providers.dashscope_chat import (
    UrllibDashScopeTransport,
    dashscope_generation_url,
)


_MAX_SEARCH_SOURCES = 20
_STREAM_END = object()


class DashScopeProviderError(RuntimeError):
    """A sanitized failure at the DashScope provider boundary."""


class DashScopeNativeChatModel(BaseChatModel):
    """Official DashScope text-generation API exposed as a BaseChatModel."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    api_key: SecretStr
    base_url: str
    model_name: str
    timeout_seconds: float = 75.0
    max_tokens: int = 1_024
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
                url=dashscope_generation_url(self.base_url),
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
        payload = self._build_payload(messages, stop=stop, stream=True, **kwargs)
        stream: Iterator[dict[str, Any]] | None = None
        sources: list[dict[str, Any]] = []
        stream_has_citations = False
        next_text_block_index = 0
        terminal_seen = False
        try:
            stream = self.http_transport.stream_sse(
                url=dashscope_generation_url(self.base_url),
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
                rendered_content = _content_with_search_citations(
                    content,
                    sources,
                    append_uncited_sources=False,
                )
                if _content_has_citations(rendered_content):
                    stream_has_citations = True
                if terminal and sources and not stream_has_citations:
                    rendered_content = _content_with_search_citations(
                        content,
                        sources,
                        append_uncited_sources=True,
                    )
                indexed_content = _indexed_stream_text_blocks(
                    rendered_content,
                    start_index=next_text_block_index,
                )
                next_text_block_index += len(indexed_content)
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=indexed_content,
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
        deep_research = kwargs.get("provider_search_profile") == "deep_research"
        parameters: dict[str, Any] = {
            "result_format": "message",
            "max_tokens": self.max_tokens,
            "enable_thinking": self.enable_thinking or deep_research,
        }
        if stream:
            parameters["incremental_output"] = True
        if stop:
            parameters["stop"] = stop
        if self.enable_search or deep_research:
            parameters["enable_search"] = True
            parameters["search_options"] = {
                "search_strategy": "max" if deep_research else "turbo",
                "forced_search": deep_research,
                "enable_search_extension": True,
                "enable_source": True,
                "enable_citation": True,
                "citation_format": "[<number>]",
                **({} if deep_research else {"freshness": 7}),
            }
        tools = kwargs.get("tools")
        if isinstance(tools, list):
            parameters["tools"] = tools
            tool_choice = kwargs.get("tool_choice")
            if tool_choice is not None:
                parameters["tool_choice"] = tool_choice
        return {
            "model": self.model_name,
            "input": {"messages": [_message_to_dashscope(item) for item in messages]},
            "parameters": parameters,
        }

    def _parse_response(self, data: dict[str, Any]) -> AIMessage:
        output, choice, raw_message = _response_parts(data)
        raw_tool_calls = raw_message.get("tool_calls")
        parsed_calls, invalid_calls = _parse_tool_calls(raw_tool_calls)
        content = _message_text(raw_message.get("content"))
        if not content and not parsed_calls and not invalid_calls:
            raise DashScopeProviderError("DashScope response was empty.")
        finish_reason = _optional_text(choice.get("finish_reason"))
        sources = _parse_search_sources(
            output.get("search_info", data.get("search_info"))
        )
        return AIMessage(
            content=_content_with_search_citations(content, sources),
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


def _message_to_dashscope(message: AnyMessage) -> dict[str, Any]:
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
        elif isinstance(block, Mapping):
            parts.append(
                json.dumps(dict(block), ensure_ascii=False, separators=(",", ":"))
            )
    return "\n".join(part for part in parts if part)


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
        raw_index = raw.get("index", position)
        chunks.append(
            tool_call_chunk(
                name=_optional_text(function.get("name") or raw.get("name")),
                args=_optional_text(function.get("arguments", raw.get("arguments"))),
                id=_optional_text(raw.get("id")),
                index=raw_index if isinstance(raw_index, int) else position,
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


def _content_with_search_citations(
    text: str,
    sources: Sequence[Mapping[str, Any]],
    *,
    append_uncited_sources: bool = True,
) -> str | list[dict[str, Any]]:
    if not sources:
        return text

    answer_annotations = []
    valid_sources: list[tuple[int, str, str]] = []
    for source in sources:
        index = source.get("index")
        title = source.get("title")
        url = source.get("url")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 1
            or not isinstance(title, str)
            or not isinstance(url, str)
        ):
            continue
        valid_sources.append((index, title, url))
        marker = f"[{index}]"
        citation_id = f"source_{index}"
        for match in re.finditer(re.escape(marker), text):
            answer_annotations.append(
                create_citation(
                    id=citation_id,
                    url=url,
                    title=title,
                    start_index=match.start(),
                    end_index=match.end(),
                    cited_text=marker,
                )
            )
    if not valid_sources:
        return text
    if answer_annotations:
        return [
            create_text_block(
                text,
                id="answer",
                annotations=answer_annotations,
            )
        ]
    if not append_uncited_sources:
        return text

    sources_text = "\n\n来源：\n" + "\n".join(
        f"[{index}] {title}" for index, title, _url in valid_sources
    )
    source_annotations = []
    search_from = 0
    for index, title, url in valid_sources:
        source_label = f"[{index}] {title}"
        start_index = sources_text.index(source_label, search_from)
        end_index = start_index + len(source_label)
        search_from = end_index
        source_annotations.append(
            create_citation(
                id=f"source_{index}",
                url=url,
                title=title,
                start_index=start_index,
                end_index=end_index,
                cited_text=source_label,
            )
        )

    blocks = []
    if text:
        blocks.append(
            create_text_block(
                text,
                id="answer",
            )
        )
    blocks.append(
        create_text_block(
            sources_text,
            id="sources",
            annotations=source_annotations,
        )
    )
    return blocks


def _content_has_citations(content: str | list[dict[str, Any]]) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, Mapping) and bool(block.get("annotations"))
        for block in content
    )


def _indexed_stream_text_blocks(
    content: str | list[dict[str, Any]],
    *,
    start_index: int,
) -> list[dict[str, Any]]:
    if isinstance(content, str):
        blocks = [create_text_block(content)] if content else []
    else:
        blocks = content
    return [
        {
            **block,
            "index": f"lc_dashscope_text_{start_index + offset}",
        }
        for offset, block in enumerate(blocks)
    ]


def _usage_metadata(value: Any) -> dict[str, int] | None:
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
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


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

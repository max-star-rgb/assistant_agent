"""Deterministic current-turn projection of native ToolMessage deliveries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from assistant_agent.tools.delivery import read_tool_delivery, safe_http_url


_MAX_TEXT_CHARS = 16_000
_MAX_OUTPUT_REFS = 4


@dataclass(frozen=True)
class TurnDelivery:
    """Validated material the transport must deliver independently of the LLM."""

    text_suffix: str = ""
    output_refs: tuple[str, ...] = ()


def turn_delivery(messages: Sequence[Any]) -> TurnDelivery:
    """Project each Tool name's latest successful result from the current turn."""

    current = _current_turn(messages)
    latest_by_name: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for index, message in enumerate(current):
        data = _message_data(message)
        if _is_tool_message(message, data) and (name := str(data.get("name") or "")):
            latest_by_name[name] = (index, data)

    text_parts: list[str] = []
    output_refs: list[str] = []
    for _, message in sorted(latest_by_name.values()):
        if not _successful(message):
            continue
        delivery = read_tool_delivery(message.get("artifact"))
        if delivery is not None:
            _append_text(text_parts, delivery.text)
            for output_ref in delivery.output_refs:
                if (
                    output_ref not in output_refs
                    and len(output_refs) < _MAX_OUTPUT_REFS
                ):
                    output_refs.append(output_ref)
        for link in _file_links(message.get("content")):
            _append_text(text_parts, link)

    return TurnDelivery("\n".join(text_parts), tuple(output_refs))


def _append_text(parts: list[str], text: str) -> None:
    if not text or text in parts:
        return
    projected_length = sum(map(len, parts)) + len(parts) + len(text)
    if projected_length <= _MAX_TEXT_CHARS:
        parts.append(text)


def _current_turn(messages: Sequence[Any]) -> list[Any]:
    for index in range(len(messages) - 1, -1, -1):
        if _is_human_message(messages[index]):
            return list(messages[index + 1 :])
    return list(messages)


def _file_links(content: Any) -> list[str]:
    if not isinstance(content, (list, tuple)):
        return []
    urls = [
        block.get("url")
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") == "file"
        and safe_http_url(block.get("url"))
    ]
    return [f"[下载文件]({url})" for url in dict.fromkeys(urls)][:4]


def _message_data(message: Any) -> Mapping[str, Any]:
    if isinstance(message, Mapping):
        return message
    if hasattr(message, "model_dump"):
        data = message.model_dump()
        return data if isinstance(data, Mapping) else {}
    return {}


def _is_tool_message(message: Any, data: Mapping[str, Any]) -> bool:
    return isinstance(message, ToolMessage) or data.get("type") in {
        "tool",
        "ToolMessage",
    }


def _is_human_message(message: Any) -> bool:
    if isinstance(message, HumanMessage):
        return True
    data = _message_data(message)
    return data.get("type") == "human" or data.get("role") == "user"


def _successful(message: Mapping[str, Any]) -> bool:
    return message.get("status") in {None, "success"}


__all__ = ["TurnDelivery", "turn_delivery"]

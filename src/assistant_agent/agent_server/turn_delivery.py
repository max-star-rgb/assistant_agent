"""Deterministic current-turn projection of ToolMessage artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import ValidationError

from assistant_agent.agent_server.shopping_detail import shopping_detail_block
from assistant_agent.media.generated_artifacts import generated_image_output_refs
from assistant_agent.mcp.amap_route_links import AMAP_NAVIGATION_ARTIFACT_KEY
from assistant_agent.tools.plugins.builtin.lodging.models import LodgingSearchResult


_AMAP_ROUTE_TOOL_SUFFIXES = (
    "maps_direction_driving",
    "maps_direction_transit_integrated",
    "maps_bicycling",
    "maps_direction_walking",
)


@dataclass(frozen=True)
class TurnDelivery:
    """Validated material the transport must deliver independently of the LLM."""

    text_suffix: str = ""
    output_refs: tuple[str, ...] = ()


def turn_delivery(messages: Sequence[Any]) -> TurnDelivery:
    """Project the latest relevant successful tools from the current user turn."""

    current = _current_turn(messages)
    text_parts: list[str] = []

    shopping = _latest_tool(current, lambda name: name == "shopping_search")
    if shopping is not None and _successful(shopping):
        detail = shopping_detail_block(current)
        if detail:
            text_parts.append(detail)

    lodging = _latest_tool(current, lambda name: name == "lodging_search")
    if lodging is not None and _successful(lodging):
        detail = _lodging_detail(lodging.get("artifact"))
        if detail:
            text_parts.append(detail)

    navigation = _latest_tool(
        current,
        lambda name: any(name.endswith(suffix) for suffix in _AMAP_ROUTE_TOOL_SUFFIXES),
    )
    if navigation is not None and _successful(navigation):
        link = _navigation_link(navigation.get("artifact"))
        if link:
            text_parts.append(link)

    file_message = _latest_file_message(current)
    if file_message is not None and _successful(file_message):
        links = _file_links(file_message.get("content"))
        if links:
            text_parts.append("\n".join(links))

    image = _latest_tool(current, lambda name: name == "image_generation")
    output_refs = (
        tuple(generated_image_output_refs([image]))
        if image is not None and _successful(image)
        else ()
    )
    return TurnDelivery(
        text_suffix="\n".join(text_parts),
        output_refs=output_refs,
    )


def _current_turn(messages: Sequence[Any]) -> list[Any]:
    for index in range(len(messages) - 1, -1, -1):
        if _is_human_message(messages[index]):
            return list(messages[index + 1 :])
    return list(messages)


def _latest_tool(
    messages: Sequence[Any],
    matches: Callable[[str], bool],
) -> Mapping[str, Any] | None:
    for message in reversed(messages):
        data = _message_data(message)
        if _is_tool_message(message, data) and matches(str(data.get("name") or "")):
            return data
    return None


def _latest_file_message(messages: Sequence[Any]) -> Mapping[str, Any] | None:
    file_tool_names = {
        str(data.get("name") or "")
        for message in messages
        if _is_tool_message(message, data := _message_data(message))
        and isinstance(data.get("content"), (list, tuple))
        and any(
            isinstance(block, Mapping) and block.get("type") == "file"
            for block in data["content"]
        )
    }
    for message in reversed(messages):
        data = _message_data(message)
        if _is_tool_message(message, data) and str(data.get("name") or "") in file_tool_names:
            return data
    return None


def _lodging_detail(artifact: Any, *, max_items: int = 3) -> str:
    if not isinstance(artifact, Mapping):
        return ""
    try:
        result = LodgingSearchResult.model_validate(artifact)
    except ValidationError:
        return ""
    if not result.success:
        return ""
    lines: list[str] = []
    for offer in result.offers:
        if not _safe_http_url(offer.booking_url):
            continue
        image = (
            f"<pic>{offer.image_url}</pic>"
            if _safe_http_url(offer.image_url)
            else ""
        )
        lines.append(
            f"{len(lines) + 1}. 酒店 - {_clean_text(offer.property_name)} "
            f"{_format_number(offer.total_price)} {offer.currency} "
            f"<link>{offer.booking_url}</link>{image}"
        )
        if len(lines) >= max_items:
            break
    return "<detail>\n" + "\n".join(lines) + "\n</detail>" if lines else ""


def _navigation_link(artifact: Any) -> str:
    if not isinstance(artifact, Mapping):
        return ""
    structured = artifact.get("structured_content")
    if not isinstance(structured, Mapping):
        return ""
    navigation = structured.get(AMAP_NAVIGATION_ARTIFACT_KEY)
    if not isinstance(navigation, Mapping):
        return ""
    url = navigation.get("url")
    return f"[打开高德地图导航]({url})" if _safe_http_url(url) else ""


def _file_links(content: Any, *, max_items: int = 4) -> list[str]:
    if not isinstance(content, (list, tuple)):
        return []
    urls = [
        block.get("url")
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") == "file"
        and _safe_http_url(block.get("url"))
    ]
    return [f"[下载文件]({url})" for url in dict.fromkeys(urls)][:max_items]


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


def _safe_http_url(value: Any) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    if any(character.isspace() or character in "<>[]()" for character in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except (UnicodeError, ValueError):
        return False


def _clean_text(value: str) -> str:
    return " ".join(value.replace("<", "").replace(">", "").split())[:240]


def _format_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


__all__ = ["AMAP_NAVIGATION_ARTIFACT_KEY", "TurnDelivery", "turn_delivery"]

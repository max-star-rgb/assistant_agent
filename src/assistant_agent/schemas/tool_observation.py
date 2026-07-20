"""Compact ReAct tool observations for assistant-loop decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.provider_errors import (
    sanitize_error_detail,
    sanitize_error_message,
)


ObservationStatus = Literal["succeeded", "failed", "rejected"]


class ToolObservation(BaseModel):
    """Assistant-facing summary of a tool result or action rejection."""

    tool_name: str = Field(min_length=1)
    status: ObservationStatus
    summary: str = Field(min_length=1)
    output_ref: str | None = None
    structured_output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    next_step_hint: str | None = None
    redacted: bool = True
    truncated: bool = False
    original_chars: int | None = Field(default=None, ge=0)


def observation_from_tool_result(
    result: ToolResult,
    *,
    request_text: str | None = None,
    prior_observations: Sequence[Mapping[str, Any]] | None = None,
    max_result_chars: int | None = None,
) -> ToolObservation:
    """Build a redacted LLM-facing observation from a ToolResult.

    Tools should populate ``model_observation`` with the fields useful for the
    main model's next decision or final answer. ``data`` remains the full tool
    result contract and is only used here as a compatibility fallback.
    """

    status: ObservationStatus = "succeeded" if result.success else "failed"
    data_source = (
        result.model_observation
        if isinstance(result.model_observation, dict)
        else result.data
    )
    data = sanitize_error_detail(data_source or {})
    error_message = sanitize_error_message(result.error or "") if result.error else None
    observation = ToolObservation(
        tool_name=result.tool_name,
        status=status,
        summary=_summary_from_result(result, data, error_message),
        output_ref=result.output_ref,
        structured_output=data if isinstance(data, dict) else {},
        error_code=_error_code(result.error),
        error_message=error_message,
        next_step_hint=_next_step_hint(
            result.tool_name,
            status,
            data=data if isinstance(data, dict) else {},
            request_text=request_text,
            prior_observations=prior_observations or (),
        ),
    )
    return _bound_observation(observation, max_result_chars=max_result_chars)


def rejected_observation(
    *,
    tool_name: str,
    error_code: str,
    error_message: str,
    next_step_hint: str | None = None,
) -> ToolObservation:
    """Build an observation for an action rejected before execution."""

    message = sanitize_error_message(error_message)
    return ToolObservation(
        tool_name=tool_name or "unknown",
        status="rejected",
        summary=f"Action rejected: {message}",
        error_code=error_code,
        error_message=message,
        next_step_hint=next_step_hint
        or "Select a valid action or ask a follow-up question.",
    )


def _summary_from_result(
    result: ToolResult, data: Any, error_message: str | None
) -> str:
    if not result.success:
        return error_message or "Tool execution failed."
    if isinstance(data, dict):
        if isinstance(result.model_observation, dict):
            explicit_summary = data.get("summary") or data.get("message")
            if isinstance(explicit_summary, str) and explicit_summary.strip():
                return sanitize_error_message(explicit_summary)
        fetch_summary = _web_fetch_summary(result.tool_name, data)
        if fetch_summary:
            return fetch_summary
        web_summary = _web_search_summary(result.tool_name, data)
        if web_summary:
            return web_summary
        product_summary = _shopping_summary(result.tool_name, data)
        if product_summary:
            return product_summary
        summary = data.get("summary") or data.get("message")
        if isinstance(summary, str) and summary.strip():
            return sanitize_error_message(summary)
        if result.output_ref:
            return f"{result.tool_name} succeeded with output {result.output_ref}."
    return f"{result.tool_name} succeeded."


def _shopping_summary(tool_name: str, data: dict[str, Any]) -> str:
    if tool_name == "shopping_search":
        best_offer = data.get("best_offer")
        if isinstance(best_offer, dict) and best_offer:
            return _format_product_item_summary(
                best_offer, prefix="Best shopping offer"
            )
    return ""


def _web_search_summary(tool_name: str, data: dict[str, Any]) -> str:
    if tool_name != "web_search":
        return ""
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return ""
    first = results[0]
    if not isinstance(first, dict):
        return ""
    title = first.get("title") or "result"
    url = first.get("url")
    source = first.get("source")
    published_at = first.get("published_at")
    total = data.get("total")
    total_part = f" of {total}" if total is not None else ""
    source_part = f", source {source}" if source else ""
    date_part = f", published_at {published_at}" if published_at else ""
    url_part = f", url {url}" if url else ", no result url"
    return sanitize_error_message(
        f"Top web result{total_part}: {title}{source_part}{date_part}{url_part}."
    )


def _web_fetch_summary(tool_name: str, data: dict[str, Any]) -> str:
    if tool_name != "web_fetch":
        return ""
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        return ""
    title = data.get("title") if isinstance(data.get("title"), str) else "web page"
    total_chars = data.get("total_chars")
    chars_part = f", content_chars {total_chars}" if total_chars is not None else ""
    truncated_part = ", truncated" if data.get("truncated") else ""
    return sanitize_error_message(
        f"Fetched web page: {title}, url {url}{chars_part}{truncated_part}."
    )


def _format_product_item_summary(
    item: dict[str, Any], *, total: Any = None, prefix: str = "Top product"
) -> str:
    title = item.get("title") or "candidate"
    price = item.get("total_price") or item.get("price")
    currency = item.get("currency") or "CNY"
    url = item.get("product_url") or item.get("url")
    url_status = item.get("url_status")
    total_part = f" of {total}" if total is not None else ""
    price_part = f", price {price} {currency}" if price is not None else ""
    if url:
        status_part = "" if url_status == "verified" else ", url_status unverified"
        url_part = f", url {url}{status_part}"
    else:
        url_part = ", no direct product url"
    return sanitize_error_message(
        f"{prefix}{total_part}: {title}{price_part}{url_part}."
    )


def _error_code(error: str | None) -> str | None:
    if not error:
        return None
    prefix = error.split(":", 1)[0].strip()
    return (
        prefix
        if prefix.startswith("provider_") or prefix.endswith("_error")
        else "tool_failed"
    )


def _next_step_hint(
    tool_name: str,
    status: ObservationStatus,
    *,
    data: dict[str, Any],
    request_text: str | None,
    prior_observations: Sequence[Mapping[str, Any]],
) -> str:
    if status != "succeeded":
        if _has_prior_successful_observation(prior_observations, tool_name):
            return (
                f"A previous {tool_name} call already succeeded. Use that earlier observation, "
                "answer with partial results, or choose a different action instead of failing the run solely on this repeat."
            )
        return "Explain the failure, use a different action, or ask the user for clarification."
    if tool_name in {"vision_understanding", "video_understanding"}:
        return (
            "If the user only asked for a description, final_answer is likely enough."
        )
    if tool_name == "shopping_search":
        return "已完成商品搜索和比价；请基于 structured_output.best_offer、offers 和 URL 状态给出最终购物建议，不要声称已经下单。"
    if tool_name == "web_search":
        return "Use the web search results in the final answer; include source URLs and published dates when present."
    if tool_name == "web_fetch":
        return "Use the fetched page content in the final answer; cite the source URL when it informs the answer."
    if tool_name == "image_generation":
        return "Return the generated image reference to the user."
    if tool_name == "render_3d":
        return "Return the 3D preview reference to the user."
    return "Use this observation to decide whether to answer or call another action."


def _has_prior_successful_observation(
    prior_observations: Sequence[Mapping[str, Any]],
    tool_name: str,
) -> bool:
    return any(
        observation.get("tool_name") == tool_name
        and observation.get("status") == "succeeded"
        for observation in prior_observations
    )


def _bound_observation(
    observation: ToolObservation,
    *,
    max_result_chars: int | None,
) -> ToolObservation:
    if max_result_chars is None:
        return observation
    original_payload = observation.model_dump(mode="json")
    original_chars = _json_chars(original_payload)
    if original_chars <= max_result_chars:
        return observation

    field_names = sorted(observation.structured_output)[:20]
    bounded = observation.model_copy(
        update={
            "structured_output": {
                "truncated": True,
                "original_chars": original_chars,
                "field_names": field_names,
                "preview": _bounded_structured_preview(observation.structured_output),
            },
            "truncated": True,
            "original_chars": original_chars,
        },
        deep=True,
    )
    if _json_chars(bounded.model_dump(mode="json")) <= max_result_chars:
        return bounded

    bounded.next_step_hint = None
    overflow = _json_chars(bounded.model_dump(mode="json")) - max_result_chars
    if overflow > 0:
        bounded.summary = _clip_to_chars(
            bounded.summary, max(1, len(bounded.summary) - overflow)
        )
    if _json_chars(bounded.model_dump(mode="json")) <= max_result_chars:
        return bounded

    bounded.error_message = None
    return bounded


def _json_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _clip_to_chars(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    return value[: max_chars - 3].rstrip() + "..."


def _bounded_structured_preview(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _clip_to_chars(value, 80)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 2:
        return "[truncated]"
    if isinstance(value, Mapping):
        preview: dict[str, Any] = {}
        for key in sorted(str(item) for item in value)[:5]:
            if key == "max_result_chars":
                continue
            preview[key] = _bounded_structured_preview(value.get(key), depth=depth + 1)
        return preview
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_structured_preview(item, depth=depth + 1)
            for item in list(value)[:2]
        ]
    return _clip_to_chars(str(value), 80)

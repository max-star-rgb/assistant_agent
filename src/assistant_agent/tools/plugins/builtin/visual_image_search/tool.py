"""Visual image search Tool backed by a Plugin-private adapter."""

from typing import Any

from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.visual_image_search.models import (
    VisualImageSearchRequest,
    VisualImageSearchResult,
)
from assistant_agent.providers.provider_errors import sanitize_error_detail
from assistant_agent.tools.plugins.builtin.visual_image_search.backend import (
    VisualImageSearchAdapter,
    create_visual_image_search_adapter,
)
from assistant_agent.tools.ids import VISUAL_IMAGE_SEARCH_CAPABILITY, VISUAL_IMAGE_SEARCH_TOOL_NAME
from assistant_agent.tools.base import ToolBase, ToolContext


class VisualImageSearchTool(ToolBase):
    name = VISUAL_IMAGE_SEARCH_TOOL_NAME
    description = "按公开图片 URL 搜索相似图片。"
    input_schema = VisualImageSearchRequest
    output_schema = VisualImageSearchResult
    category = "read"
    repeat_policy = "distinct_inputs"
    requires_media = ["image"]
    llm_hidden_input_fields = ("limit",)

    def __init__(self, adapter: VisualImageSearchAdapter | None = None) -> None:
        self.adapter = adapter or create_visual_image_search_adapter()

    def _run(self, input: VisualImageSearchRequest, context: ToolContext) -> ToolResult:
        result = self.adapter.search(input)
        data = result.model_dump(mode="json")
        model_observation = _visual_image_search_model_observation(data)
        contract = build_capability_output_contract(
            capability=VISUAL_IMAGE_SEARCH_CAPABILITY,
            status="failed" if result.errors else "succeeded",
            output_ref=result.output_ref,
            data={
                "image_used": result.image_used,
                "query_hint_used": result.query_hint_used,
                "matches": data.get("matches", []),
                "total": result.total,
            },
            errors=[error.model_dump(mode="json") for error in result.errors],
            metadata={"provider": result.provider, "latency_ms": result.latency_ms},
        )
        if result.errors:
            first_error = result.errors[0]
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=data,
                model_observation=model_observation,
                error=f"{first_error.code}: {first_error.message}",
                output_ref=result.output_ref,
                latency_ms=result.latency_ms,
                contract=contract,
            )

        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation=model_observation,
            output_ref=result.output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
        )


def _visual_image_search_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "summary": _visual_image_search_summary(data),
        "image_used": data.get("image_used"),
        "query_hint_used": data.get("query_hint_used"),
        "total": data.get("total"),
        "matches": [
            _match_model_observation(item)
            for item in data.get("matches", [])
            if isinstance(item, dict)
        ],
    }
    errors = data.get("errors")
    if errors:
        observation["errors"] = errors
    compact = {
        key: value
        for key, value in observation.items()
        if value not in (None, "", [], {})
    }
    return sanitize_error_detail(compact)


def _visual_image_search_summary(data: dict[str, Any]) -> str:
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "Visual image search failed.")
    total = data.get("total")
    if not isinstance(total, int):
        total = len(data.get("matches") or [])
    return f"Found {total} visually similar image result(s)."


def _match_model_observation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "title",
            "page_url",
            "image_url",
            "thumbnail_url",
            "source",
            "snippet",
            "similarity_score",
        )
        if item.get(key) not in (None, "", [], {})
    }

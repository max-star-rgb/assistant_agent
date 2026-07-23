"""Web search tool backed by a provider adapter."""

from typing import Any

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.web_search import WebSearchRequest, WebSearchResult
from assistant_agent.services.web_search_adapter import (
    WebSearchAdapter,
    create_web_search_adapter,
)
from assistant_agent.schemas.tool_ids import WEB_SEARCH_CAPABILITY, WEB_SEARCH_TOOL_NAME
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import ToolInputBinding


class WebSearchTool(ToolBase):
    name = WEB_SEARCH_TOOL_NAME
    description = "当专用个人工具无法覆盖请求时，搜索公开网页中的当前信息。"
    input_schema = WebSearchRequest
    output_schema = WebSearchResult
    category = "read"
    requires_confirmation = False
    input_bindings = (
        ToolInputBinding(field="limit", source="constant", value=5),
    )

    def __init__(self, adapter: WebSearchAdapter | None = None) -> None:
        self.adapter = adapter or create_web_search_adapter()

    def _run(self, input: WebSearchRequest, context: ToolContext) -> ToolResult:
        result = self.adapter.search(input)
        data = result.model_dump(mode="json")
        model_observation = _web_search_model_observation(data)
        contract = build_capability_output_contract(
            capability=WEB_SEARCH_CAPABILITY,
            status="failed" if result.errors else "succeeded",
            output_ref=result.output_ref,
            data={
                "query_used": result.query_used,
                "results": data.get("results", []),
                "summary": result.summary,
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


def _web_search_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "query_used": data.get("query_used"),
        "total": data.get("total"),
        "results": [
            _web_result_model_observation(item)
            for item in data.get("results", [])
            if isinstance(item, dict)
        ],
    }
    errors = data.get("errors")
    if errors:
        observation["errors"] = errors
    return {
        key: value for key, value in observation.items() if value not in (None, [], {})
    }


def _web_result_model_observation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("title", "url", "snippet", "source", "published_at")
        if item.get(key) not in (None, "", [], {})
    }

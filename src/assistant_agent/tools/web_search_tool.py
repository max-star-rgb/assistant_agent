"""Web search tool backed by a provider adapter."""

from typing import Any

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.web_search import WebSearchInput, WebSearchResult
from assistant_agent.services.web_search_adapter import (
    WebSearchAdapter,
    create_web_search_adapter,
)
from assistant_agent.services.tool_manifest import WEB_SEARCH_CAPABILITY, WEB_SEARCH_TOOL_NAME
from assistant_agent.tools.base import MockTool, ToolContext


class WebSearchTool(MockTool):
    name = WEB_SEARCH_TOOL_NAME
    description = (
        "Search public web pages for current facts when no dedicated personal tool covers the request."
    )
    input_schema = WebSearchInput
    output_schema = WebSearchResult
    category = "read"
    requires_confirmation = False
    allowed_entry_profiles = ["agent_service"]
    progress_message = "我联网查一下。"

    def __init__(self, adapter: WebSearchAdapter | None = None) -> None:
        self.adapter = adapter or create_web_search_adapter()

    def _run(self, input: WebSearchInput, context: ToolContext) -> ToolResult:
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

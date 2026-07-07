"""Web search tool backed by a provider adapter."""

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.web_search import WebSearchInput, WebSearchResult
from assistant_agent.services.web_search_adapter import (
    WebSearchAdapter,
    create_web_search_adapter,
)
from assistant_agent.tools.base import MockTool, ToolContext


class WebSearchTool(MockTool):
    name = "web_search"
    description = (
        "Search the web for current, latest, news, or time-sensitive information."
    )
    input_schema = WebSearchInput
    output_schema = WebSearchResult

    def __init__(self, adapter: WebSearchAdapter | None = None) -> None:
        self.adapter = adapter or create_web_search_adapter()

    def _run(self, input: WebSearchInput, context: ToolContext) -> ToolResult:
        result = self.adapter.search(input)
        data = result.model_dump(mode="json")
        contract = build_capability_output_contract(
            capability="web_search",
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
                error=f"{first_error.code}: {first_error.message}",
                output_ref=result.output_ref,
                latency_ms=result.latency_ms,
                contract=contract,
            )

        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            output_ref=result.output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
        )

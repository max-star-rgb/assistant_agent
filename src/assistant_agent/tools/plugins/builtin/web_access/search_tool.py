"""Web search Tool backed by a Plugin-private adapter."""

from typing import Any

from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.web_access.search_models import WebSearchRequest, WebSearchResult
from assistant_agent.tools.plugins.builtin.web_access.search_backend import (
    WebSearchAdapter,
    create_web_search_adapter,
)
from assistant_agent.tools.ids import WEB_SEARCH_CAPABILITY, WEB_SEARCH_TOOL_NAME
from assistant_agent.tools.base import ToolBase, ToolContext


class WebSearchTool(ToolBase):
    name = WEB_SEARCH_TOOL_NAME
    description = "搜索公开网页中的当前信息、新闻和资料。"
    input_schema = WebSearchRequest
    output_schema = WebSearchResult
    category = "read"
    repeat_policy = "distinct_inputs"
    llm_hidden_input_fields = ("limit",)

    def __init__(self, adapter: WebSearchAdapter | None = None) -> None:
        self.adapter = adapter or create_web_search_adapter()

    def _run(self, input: WebSearchRequest, context: ToolContext) -> ToolResult:
        result = self.adapter.search(input)
        data = result.model_dump(mode="json")
        model_observation = _web_search_model_observation(data)
        contract = build_capability_output_contract(
            capability=WEB_SEARCH_CAPABILITY,
            status="failed" if not result.success else "succeeded",
            output_ref=result.output_ref,
            data={
                "outcome": result.outcome,
                "query_used": result.query_used,
                "results": data.get("results", []),
                "summary": result.summary,
                "total": result.total,
            },
            errors=[error.model_dump(mode="json") for error in result.errors],
            metadata={"provider": result.provider, "latency_ms": result.latency_ms},
        )
        if not result.success:
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
        "summary": _web_search_summary(data),
        "outcome": data.get("outcome"),
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


def _web_search_summary(data: dict[str, Any]) -> str:
    explicit = data.get("summary")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    results = data.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        title = results[0].get("title") or "未命名结果"
        return f"找到 {len(results)} 条网页结果，首条为“{title}”。"
    if data.get("outcome") == "failed":
        return "网页搜索执行失败。"
    return "网页搜索完成，但没有找到结果。"


def _web_result_model_observation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("title", "url", "snippet", "source", "published_at")
        if item.get(key) not in (None, "", [], {})
    }

"""Web fetch tool backed by a provider adapter."""

from typing import Any

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.web_fetch import WebFetchInput, WebFetchResult
from assistant_agent.services.web_fetch_adapter import (
    WebFetchAdapter,
    create_web_fetch_adapter,
)
from assistant_agent.services.tool_manifest import WEB_FETCH_CAPABILITY, WEB_FETCH_TOOL_NAME
from assistant_agent.tools.base import MockTool, ToolContext


class WebFetchTool(MockTool):
    name = WEB_FETCH_TOOL_NAME
    description = (
        "Fetch readable page content from a specific HTTP(S) URL."
    )
    input_schema = WebFetchInput
    output_schema = WebFetchResult
    category = "read"
    requires_confirmation = False
    allowed_entry_profiles = ["agent_service"]
    progress_message = "我打开这个网页看一下。"

    def __init__(self, adapter: WebFetchAdapter | None = None) -> None:
        self.adapter = adapter or create_web_fetch_adapter()

    def _run(self, input: WebFetchInput, context: ToolContext) -> ToolResult:
        result = self.adapter.fetch(input)
        data = result.model_dump(mode="json")
        model_observation = _web_fetch_model_observation(data)
        contract = build_capability_output_contract(
            capability=WEB_FETCH_CAPABILITY,
            status="failed" if result.errors else "succeeded",
            output_ref=result.output_ref,
            data={
                "url": result.url,
                "title": result.title,
                "content": result.content,
                "content_format": result.content_format,
                "total_chars": result.total_chars,
                "truncated": result.truncated,
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


def _web_fetch_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "url": data.get("url"),
        "title": data.get("title"),
        "content": data.get("content"),
        "content_format": data.get("content_format"),
        "total_chars": data.get("total_chars"),
        "truncated": data.get("truncated"),
    }
    errors = data.get("errors")
    if errors:
        observation["errors"] = errors
    return {
        key: value for key, value in observation.items() if value not in (None, [], {})
    }

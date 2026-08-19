"""Native visual image search Tool backed by a Plugin-private adapter."""

from typing import Annotated, Any

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.visual_image_search.models import (
    VisualImageSearchRequest,
)
from assistant_agent.providers.provider_errors import sanitize_error_detail
from assistant_agent.tools.plugins.builtin.visual_image_search.backend import (
    VisualImageSearchAdapter,
    create_visual_image_search_adapter,
)
from assistant_agent.tools.ids import (
    VISUAL_IMAGE_SEARCH_CAPABILITY,
    VISUAL_IMAGE_SEARCH_TOOL_NAME,
)
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    invoke_native_tool,
)
from assistant_agent.tools.runtime import ToolContext, tool_context


def create_visual_image_search_tool(
    adapter: VisualImageSearchAdapter | None = None,
) -> BaseTool:
    """Create a native read-only visual image search Tool."""

    search_adapter = adapter or create_visual_image_search_adapter()

    @tool(VISUAL_IMAGE_SEARCH_TOOL_NAME, response_format="content_and_artifact")
    def visual_image_search(
        runtime: ToolRuntime[AssistantRunContext],
        image_url: Annotated[
            str | None,
            Field(default=None, description="公开 HTTP(S) 图片 URL。"),
        ] = None,
        image_ids: Annotated[
            list[str],
            Field(description="公开图片 URL 列表，使用首项。"),
        ] = [],
        query_hint: Annotated[
            str | None,
            Field(default=None, description="相似搜索提示。"),
        ] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """使用公开 HTTP(S) 图片 URL 检索视觉相似图片。

        返回匹配图片、来源页面、摘要和可选相似度。只读，不理解图片内容，也不支持
        本地路径、私有媒体 ID 或 base64。
        """

        return invoke_native_tool(
            VISUAL_IMAGE_SEARCH_TOOL_NAME,
            lambda: _execute_visual_image_search(
                search_adapter,
                VisualImageSearchRequest(
                    image_url=image_url,
                    image_ids=image_ids,
                    query_hint=query_hint,
                ),
                tool_context(runtime),
            ),
        )

    return configure_builtin_tool(visual_image_search, "read")


def _execute_visual_image_search(
    adapter: VisualImageSearchAdapter,
    input: VisualImageSearchRequest,
    context: ToolContext,
) -> ToolResult:
    del context
    result = adapter.search(input)
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
            tool_name=VISUAL_IMAGE_SEARCH_TOOL_NAME,
            success=False,
            data=data,
            model_observation=model_observation,
            error=f"{first_error.code}: {first_error.message}",
            output_ref=result.output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
        )

    return ToolResult(
        tool_name=VISUAL_IMAGE_SEARCH_TOOL_NAME,
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

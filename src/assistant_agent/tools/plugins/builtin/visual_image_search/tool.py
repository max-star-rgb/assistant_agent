"""Native visual image search Tool backed by a Plugin-private adapter."""

from typing import Annotated, Any

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.plugins.builtin.visual_image_search.models import (
    VisualImageSearchRequest,
    VisualImageSearchResult,
)
from assistant_agent.providers.provider_errors import sanitize_error_detail
from assistant_agent.tools.plugins.builtin.visual_image_search.backend import (
    MockVisualImageSearchAdapter,
    VisualImageSearchAdapter,
)
from assistant_agent.tools.ids import (
    VISUAL_IMAGE_SEARCH_TOOL_NAME,
)
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
    native_tool_exception,
)


def create_visual_image_search_tool(
    adapter: VisualImageSearchAdapter | None = None,
) -> BaseTool:
    """Create a native read-only visual image search Tool."""

    search_adapter = adapter or MockVisualImageSearchAdapter()

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

        try:
            result = _execute_visual_image_search(
                search_adapter,
                VisualImageSearchRequest(
                    image_url=image_url,
                    image_ids=image_ids,
                    query_hint=query_hint,
                ),
            )
            if result.errors:
                error = result.errors[0]
                raise RuntimeError(f"{error.code}: {error.message}")
            data = result.model_dump(mode="json")
            return native_content_and_artifact(
                _visual_image_search_model_observation(data), data
            )
        except Exception as exc:
            raise native_tool_exception(exc) from exc

    return configure_builtin_tool(visual_image_search)


def _execute_visual_image_search(
    adapter: VisualImageSearchAdapter,
    input: VisualImageSearchRequest,
) -> VisualImageSearchResult:
    return adapter.search(input)


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

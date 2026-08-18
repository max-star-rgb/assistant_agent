"""PyCharm-runnable fixed-input smoke for visual_memory_search."""

from _smoke_runner import PROJECT_ROOT, run_tool_smoke

from assistant_agent.media.video.semantic_store_pool import SessionVisualSemanticStorePool
from assistant_agent.media.video.visual_memory_index import UnavailableVisualMemoryTextIndex
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    create_visual_memory_search_tool,
)


FIXED_INPUT = {"query": "钥匙在哪里？"}


if __name__ == "__main__":
    pool = SessionVisualSemanticStorePool(
        root=PROJECT_ROOT / ".data" / "evals" / "system" / "tools" / "visual-memory-smoke"
    )
    tool = create_visual_memory_search_tool(
        semantic_store_pool=pool,
        text_index=UnavailableVisualMemoryTextIndex(
            code="tool_smoke_unavailable",
            message="固定冒烟输入没有预置视觉历史。",
        ),
    )
    raise SystemExit(
        run_tool_smoke(
            tool,
            FIXED_INPUT,
            run_context=AssistantRunContext(
                entry_profile="system_eval",
                realtime_media_mode="video",
            ),
            cleanup=pool.close,
        )
    )

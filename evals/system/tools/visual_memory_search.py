"""PyCharm-runnable fixed-input smoke for visual_memory_search."""

from _smoke_runner import PROJECT_ROOT, run_tool_smoke

from assistant_agent.media.video.semantic_store_pool import SessionVisualSemanticStorePool
from assistant_agent.media.video.visual_memory_index import UnavailableVisualMemoryTextIndex
from assistant_agent.media.visual_perception.module import LiveViewProjection
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    create_visual_memory_search_tool,
)


FIXED_INPUT = {"query": "钥匙在哪里？"}
FIXED_REQUEST = [
    {"type": "text", "text": "请查找之前看到的钥匙。"},
    {
        "type": "video",
        "source": "live_camera",
        "id": "tool-smoke-live-video",
        "window_id": "tool-smoke-window",
        "window_start_sequence": 1,
        "target_sequence": 3,
    },
]
FIXED_LIVE_VIEW = LiveViewProjection(
    live_video_ids=("tool-smoke-live-video",),
    window_id="tool-smoke-window",
    window_start_sequence=1,
    target_sequence=3,
    target_video_id="tool-smoke-live-video",
)


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
        live_view_resolver=lambda *_: FIXED_LIVE_VIEW,
    )
    raise SystemExit(
        run_tool_smoke(
            tool,
            FIXED_INPUT,
            request_content=FIXED_REQUEST,
            cleanup=pool.close,
        )
    )

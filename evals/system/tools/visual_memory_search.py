"""PyCharm-runnable fixed-input smoke for visual_memory_search."""

from _smoke_runner import PROJECT_ROOT, run_tool_smoke

from assistant_agent.media.video.semantic_store_pool import SessionVisualSemanticStorePool
from assistant_agent.media.video.visual_memory_index import UnavailableVisualMemoryTextIndex
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import VisualMemorySearchTool


FIXED_INPUT = {"query": "钥匙在哪里？"}


if __name__ == "__main__":
    pool = SessionVisualSemanticStorePool(
        root=PROJECT_ROOT / ".data" / "evals" / "system" / "tools" / "visual-memory-smoke"
    )
    tool = VisualMemorySearchTool(
        semantic_store_pool=pool,
        text_index=UnavailableVisualMemoryTextIndex(
            code="tool_smoke_unavailable",
            message="固定冒烟输入没有预置视觉历史。",
        ),
    )
    raise SystemExit(run_tool_smoke(tool, FIXED_INPUT, cleanup=pool.close))

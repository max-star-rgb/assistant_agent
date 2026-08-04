"""Controlled visual-history success environment."""

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.ids import VISUAL_MEMORY_SEARCH_TOOL_NAME
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import VisualMemorySearchInput
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import AssertionResult
from evals.agent.environment_base import ControlledTaskEnvironment
from evals.agent.grading import rule_assertion
from evals.agent.task_support import (
    ControlledVisualMemorySearchTool,
    build_controlled_registry,
    install_controlled_visual_semantic_history,
)


RESULT = {
    "status": "confirmed",
    "verification_status": "succeeded",
    "matches": [
        {
            "image_observation_id": "frame-42",
            "video_id": "session-video",
            "frame_sequence": 42,
            "captured_at_ms": 1722765480000,
            "similarity": 0.91
        }
    ],
    "verified_scene": "厨房台面",
    "verified_objects": ["一串钥匙"],
    "errors": []
}


class VisualMemoryLastSeenObjectEnvironment(ControlledTaskEnvironment):
    dependency_label = "controlled:visual-semantic-memory-confirmed-v2"
    tool_catalog_label = "complete-controlled-catalog-with-visual-memory"

    def build_registry(self) -> ToolRegistry:
        return build_controlled_registry(
            config=ProviderConfig(provider_mode="mock"),
            replacements={
                VISUAL_MEMORY_SEARCH_TOOL_NAME: ControlledVisualMemorySearchTool(RESULT)
            },
        )

    def required_successes(self) -> tuple[str, ...]:
        return (VISUAL_MEMORY_SEARCH_TOOL_NAME,)

    def before_run(self, runtime, request) -> None:
        install_controlled_visual_semantic_history(
            runtime,
            request,
            summary="厨房台面上有一串钥匙。",
        )

    def task_validation_checks(self, registry: ToolRegistry) -> dict[str, AssertionResult]:
        result = registry.get(VISUAL_MEMORY_SEARCH_TOOL_NAME).run(
            VisualMemorySearchInput(query="钥匙"),
            ToolContext(user_id="eval", session_id="eval"),
        )
        return {
            "controlled_visual_result_is_confirmed": rule_assertion(
                result.success
                and result.data is not None
                and result.data.get("status") == "confirmed"
                and result.data.get("verified_scene") == "厨房台面",
                f"result={result.data}",
                label="受控视觉历史提供已确认的时间与场景",
            ),
            "visual_memory_tool_is_read_only": rule_assertion(
                registry.get_spec(VISUAL_MEMORY_SEARCH_TOOL_NAME).category == "read",
                f"category={registry.get_spec(VISUAL_MEMORY_SEARCH_TOOL_NAME).category}",
                label="视觉历史工具保持只读治理类别",
            ),
        }

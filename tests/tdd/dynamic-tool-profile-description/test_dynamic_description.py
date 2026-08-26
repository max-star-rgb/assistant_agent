from assistant_agent.native_agent.tool_profiles import (
    ToolProfile,
    ToolProfileMiddleware,
)


def test_activation_tool_describes_the_current_profile_catalog() -> None:
    middleware = ToolProfileMiddleware(
        [
            ToolProfile(
                profile_id="custom-scene",
                description="处理新增的专项场景。",
                tool_names=("custom_tool",),
            )
        ]
    )

    assert middleware.tools[0].description == (
        "当任务需要特定场景的专用工具时，加载对应 Tool Profile，再继续完成任务。"
        "仅在当前工具不足时按需加载。\n\n"
        "当前可用 Tool Profile：\n"
        "- custom-scene: 处理新增的专项场景。"
    )

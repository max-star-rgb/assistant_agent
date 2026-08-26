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


def test_activation_tool_limits_profile_id_to_the_current_catalog() -> None:
    middleware = ToolProfileMiddleware(
        [
            ToolProfile(
                profile_id="first-scene",
                description="处理第一个场景。",
                tool_names=("first_tool",),
            ),
            ToolProfile(
                profile_id="second-scene",
                description="处理第二个场景。",
                tool_names=("second_tool",),
            ),
        ]
    )

    profile_schema = middleware.tools[0].tool_call_schema.model_json_schema()[
        "properties"
    ]["profile_id"]

    assert profile_schema["description"] == "从当前可用 Tool Profile 中选择要加载的场景。"
    assert profile_schema["enum"] == ["first-scene", "second-scene"]
    assert "pattern" not in profile_schema
    assert "minLength" not in profile_schema
    assert "maxLength" not in profile_schema

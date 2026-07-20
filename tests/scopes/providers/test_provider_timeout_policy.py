from assistant_agent.services.provider_policy import TimeoutPolicy


def test_timeout_policy_defaults_are_conservative() -> None:
    policy = TimeoutPolicy()

    assert policy.default_provider_timeout_seconds == 30.0
    assert policy.for_capability("direct_chat") == 30.0
    assert policy.for_capability("image_generation") == 60.0
    assert policy.for_capability("image_understanding") == 60.0
    assert policy.for_capability("video_understanding") == 120.0
    assert policy.for_capability("shopping_search") == 20.0
    assert policy.for_capability("render_3d") == 120.0


def test_timeout_policy_reads_environment_overrides() -> None:
    policy = TimeoutPolicy.from_env(
        {
            "MULTIMODAL_AGENT_DEFAULT_PROVIDER_TIMEOUT_SECONDS": "11",
            "MULTIMODAL_AGENT_CHAT_TIMEOUT_SECONDS": "12",
            "MULTIMODAL_AGENT_IMAGE_TIMEOUT_SECONDS": "13",
            "MULTIMODAL_AGENT_VISION_TIMEOUT_SECONDS": "14",
            "MULTIMODAL_AGENT_VIDEO_TIMEOUT_SECONDS": "15",
            "MULTIMODAL_AGENT_SEARCH_TIMEOUT_SECONDS": "16",
            "MULTIMODAL_AGENT_RENDER_TIMEOUT_SECONDS": "17",
        }
    )

    assert policy.for_capability(None) == 11.0
    assert policy.for_capability("direct_chat") == 12.0
    assert policy.for_capability("image_generation") == 13.0
    assert policy.for_capability("image_understanding") == 14.0
    assert policy.for_capability("video_understanding") == 15.0
    assert policy.for_capability("shopping_search") == 16.0
    assert policy.for_capability("render_3d") == 17.0


def test_timeout_policy_ignores_invalid_env_values() -> None:
    policy = TimeoutPolicy.from_env({"MULTIMODAL_AGENT_VIDEO_TIMEOUT_SECONDS": "-1"})

    assert policy.for_capability("video_understanding") == 120.0

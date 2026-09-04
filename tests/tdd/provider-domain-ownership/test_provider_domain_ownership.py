from importlib.util import find_spec


def test_modality_adapters_live_with_their_domain_owners() -> None:
    expected_modules = (
        "assistant_agent.media.vision.ark_adapter",
        "assistant_agent.media.vision.provider_selection",
        "assistant_agent.media.video.qwen_realtime_adapter",
        "assistant_agent.tools.plugins.builtin.image_generation.ark_adapter",
        "assistant_agent.tools.plugins.builtin.image_generation.qwen_adapter",
        "assistant_agent.tools.plugins.builtin.image_generation.prompting",
    )

    assert [name for name in expected_modules if find_spec(name) is None] == []

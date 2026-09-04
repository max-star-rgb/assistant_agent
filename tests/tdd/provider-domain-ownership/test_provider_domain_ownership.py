from importlib.util import find_spec

from assistant_agent.config import ImageGenerationConfig, VisionConfig
from assistant_agent.media.video.video_adapter import (
    create_realtime_video_understanding_adapter,
)
from assistant_agent.media.vision.provider_selection import create_vision_adapter
from assistant_agent.tools.plugins.builtin.image_generation.backend import (
    create_image_generation_adapter,
)


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
    assert all(
        find_spec(name) is None
        for name in (
            "assistant_agent.providers.ark_image_generation",
            "assistant_agent.providers.ark_vision",
            "assistant_agent.providers.prompting",
            "assistant_agent.providers.provider_selection",
            "assistant_agent.providers.qwen_image_generation",
            "assistant_agent.providers.qwen_realtime_vision",
        )
    )


def test_real_adapter_factories_load_domain_owned_modules_without_io() -> None:
    qwen_image = create_image_generation_adapter(
        ImageGenerationConfig(
            image_generation_provider="qwen",
            image_generation_api_key="sentinel",
            image_generation_base_url="https://image.example",
            image_generation_model="qwen-image",
        ),
        provider_mode="real",
    )
    ark_image = create_image_generation_adapter(
        ImageGenerationConfig(
            image_generation_provider="ark",
            image_generation_api_key="sentinel",
            image_generation_base_url="https://image.example",
            image_generation_model="ark-image",
        ),
        provider_mode="real",
    )
    ark_vision = create_vision_adapter(
        VisionConfig(
            vision_provider="ark",
            vision_api_key="sentinel",
            vision_base_url="https://vision.example",
            vision_model="ark-vision",
        ),
        provider_mode="real",
    )
    qwen_realtime = create_realtime_video_understanding_adapter(
        VisionConfig(
            vision_provider="qwen",
            vision_api_key="sentinel",
            vision_base_url="https://vision.example",
            vision_model="qwen-vision",
            qwen_realtime_vision_api_key="sentinel",
        ),
        provider_mode="real",
    )

    assert type(qwen_image).__module__.endswith("image_generation.qwen_adapter")
    assert type(ark_image).__module__.endswith("image_generation.ark_adapter")
    assert type(ark_vision).__module__ == "assistant_agent.media.vision.ark_adapter"
    assert type(qwen_realtime).__module__ == (
        "assistant_agent.media.video.qwen_realtime_adapter"
    )

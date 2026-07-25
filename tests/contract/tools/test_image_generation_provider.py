"""Provider request contracts discovered by real image-generation evals."""

from assistant_agent.providers.qwen_image_generation import normalize_qwen_image_size


def test_qwen_image_2_normalizes_vertical_ratio_to_provider_size() -> None:
    assert normalize_qwen_image_size(
        "9:16",
        model="qwen-image-2.0-pro",
    ) == "1536*2688"


def test_legacy_qwen_image_normalizes_vertical_ratio_to_supported_preset() -> None:
    assert normalize_qwen_image_size(
        "9:16",
        model="qwen-image-plus",
    ) == "928*1664"

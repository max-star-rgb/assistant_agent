from __future__ import annotations

from pathlib import Path

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.context.token_counter import create_visual_context_token_counter
from assistant_agent.media.video.visual_context_compactor import (
    LLMVisualContextCompactor,
    create_visual_context_compactor,
)
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.registry import ToolRegistry


class RealChatAdapter:
    provider = "openai"
    model = "visual-compactor-test-model"


@pytest.mark.parametrize(
    ("target", "trigger", "hard"),
    [
        (0.0, 0.70, 0.85),
        (0.70, 0.70, 0.85),
        (0.40, 0.85, 0.85),
        (0.40, 0.70, 1.01),
    ],
)
def test_visual_context_compaction_ratios_must_be_strictly_ordered(
    target: float,
    trigger: float,
    hard: float,
) -> None:
    with pytest.raises(ValueError, match="visual context compaction ratios"):
        ProviderConfig(
            visual_context_compaction_target_ratio=target,
            visual_context_compaction_trigger_ratio=trigger,
            visual_context_compaction_hard_ratio=hard,
        )


def test_real_llm_visual_context_compaction_requires_its_own_tokenizer() -> None:
    config = _real_config(
        visual_context_compactor_mode="llm",
        visual_context_tokenizer_path=None,
    )

    with pytest.raises(ValueError, match="REALTIME_VISUAL_CONTEXT_TOKENIZER_PATH"):
        AgentGraphRuntime(
            config=config,
            registry=ToolRegistry(),
            chat_adapter=RealChatAdapter(),
        )


def test_mock_mode_neither_loads_visual_tokenizer_nor_creates_llm_compactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_loaded(*_: object, **__: object) -> object:
        raise AssertionError("mock mode must not load a tokenizer")

    monkeypatch.setattr(
        "assistant_agent.context.token_counter.TokenizerJsonTokenCounter",
        fail_if_loaded,
    )
    config = ProviderConfig(
        provider_mode="mock",
        visual_context_compactor_mode="llm",
        visual_context_tokenizer_path="would-download/tokenizer.json",
    )

    token_counter = create_visual_context_token_counter(config)
    compactor = create_visual_context_compactor(
        config,
        RealChatAdapter(),
        token_counter=token_counter,
    )

    assert token_counter is None
    assert compactor is None


def test_real_llm_visual_factories_use_independent_local_tokenizer(
    tmp_path: Path,
) -> None:
    tokenizer_path = tmp_path / "tokenizer.json"
    tokenizer_path.write_text(
        '{"version":"1.0","truncation":null,"padding":null,'
        '"added_tokens":[],"normalizer":null,"pre_tokenizer":null,'
        '"post_processor":null,"decoder":null,'
        '"model":{"type":"WordLevel","vocab":{"[UNK]":0},'
        '"unk_token":"[UNK]"}}',
        encoding="utf-8",
    )
    config = _real_config(
        visual_context_compactor_mode="llm",
        visual_context_tokenizer_path=str(tokenizer_path),
        context_tokenizer_path="different-chat-tokenizer.json",
    )

    token_counter = create_visual_context_token_counter(config)
    compactor = create_visual_context_compactor(
        config,
        RealChatAdapter(),
        token_counter=token_counter,
    )

    assert token_counter is not None
    assert token_counter.tokenizer_id == "visual-compactor-test-model"
    assert isinstance(compactor, LLMVisualContextCompactor)


def test_visual_context_environment_uses_realtime_prefix() -> None:
    config = ProviderConfig.from_env(
        {
            "REALTIME_VISUAL_CONTEXT_COMPACTOR": "llm",
            "REALTIME_VISUAL_CONTEXT_TOKENIZER_PATH": "/models/vlm-tokenizer.json",
            "REALTIME_VISUAL_CONTEXT_INPUT_TOKEN_LIMIT": "40000",
            "REALTIME_VISUAL_CONTEXT_COMPACTION_TARGET_RATIO": "0.30",
            "REALTIME_VISUAL_CONTEXT_COMPACTION_TRIGGER_RATIO": "0.60",
            "REALTIME_VISUAL_CONTEXT_COMPACTION_HARD_RATIO": "0.90",
            "REALTIME_VISUAL_CONTEXT_SAFETY_MARGIN_TOKENS": "1000",
            "REALTIME_VISUAL_CONTEXT_SUMMARY_MAX_TOKENS": "1500",
            "REALTIME_VISUAL_CONTEXT_KEEP_RECENT_RECORDS": "6",
            "REALTIME_VISUAL_CONTEXT_INSTRUCTION_RESERVE_TOKENS": "900",
            "REALTIME_VISUAL_CONTEXT_IMAGE_RESERVE_TOKENS": "1900",
            "REALTIME_VISUAL_CONTEXT_OUTPUT_RESERVE_TOKENS": "1800",
        }
    )

    assert config.visual_context_compactor_mode == "llm"
    assert config.visual_context_tokenizer_path == "/models/vlm-tokenizer.json"
    assert config.visual_context_input_token_limit == 40_000
    assert config.visual_context_compaction_target_ratio == 0.30
    assert config.visual_context_compaction_trigger_ratio == 0.60
    assert config.visual_context_compaction_hard_ratio == 0.90
    assert config.visual_context_compaction_safety_margin_tokens == 1_000
    assert config.visual_context_summary_max_tokens == 1_500
    assert config.visual_context_keep_recent_records == 6
    assert config.visual_context_instruction_reserve_tokens == 900
    assert config.visual_context_image_reserve_tokens == 1_900
    assert config.visual_context_output_reserve_tokens == 1_800


def test_runtime_creates_visual_context_dependencies_without_context_injection() -> (
    None
):
    config = ProviderConfig(
        visual_context_compaction_target_ratio=0.30,
        visual_context_compaction_trigger_ratio=0.60,
        visual_context_compaction_hard_ratio=0.90,
        visual_context_input_token_limit=40_000,
        visual_context_compaction_safety_margin_tokens=1_000,
        visual_context_summary_max_tokens=1_500,
    )
    runtime = AgentGraphRuntime(config=config, registry=ToolRegistry())
    try:
        assert runtime.visual_context_token_counter is None
        assert runtime.visual_context_compactor is None
        assert runtime.visual_context_window_policy.input_token_limit == 40_000
        assert runtime.visual_context_window_policy.target_ratio == 0.30
        assert runtime.visual_context_window_policy.trigger_ratio == 0.60
        assert runtime.visual_context_window_policy.hard_ratio == 0.90
        assert runtime.visual_context_window_policy.safety_margin_tokens == 1_000
        assert runtime.visual_context_window_policy.summary_max_tokens == 1_500
        assert not hasattr(runtime.context_service, "visual_context_compactor")
    finally:
        runtime.close()


def _real_config(**overrides: object) -> ProviderConfig:
    return ProviderConfig(
        provider_mode="real",
        chat_provider="openai",
        chat_api_key="test-key",
        chat_base_url="https://example.invalid/v1",
        chat_model="visual-compactor-test-model",
        **overrides,
    )

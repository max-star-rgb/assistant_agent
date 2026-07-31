from assistant_agent.config import ProviderConfig
from scripts.migrate_mem0_memories_to_chinese import (
    create_memory_translation_adapter,
    migration_apply_gate_error,
)


def test_apply_gate_requires_real_qwen_mem0_and_operator_confirmation() -> None:
    assert migration_apply_gate_error(
        provider_mode="mock",
        chat_provider="qwen",
        mem0_base_url="http://mem0.test",
        allow_real_provider=True,
    ) == "real_provider_mode_required"
    assert migration_apply_gate_error(
        provider_mode="real",
        chat_provider="mock",
        mem0_base_url="http://mem0.test",
        allow_real_provider=True,
    ) == "qwen_provider_required"
    assert migration_apply_gate_error(
        provider_mode="real",
        chat_provider="qwen",
        mem0_base_url=None,
        allow_real_provider=True,
    ) == "mem0_base_url_required"
    assert migration_apply_gate_error(
        provider_mode="real",
        chat_provider="qwen",
        mem0_base_url="http://mem0.test",
        allow_real_provider=False,
    ) == "operator_confirmation_required"
    assert migration_apply_gate_error(
        provider_mode="real",
        chat_provider="qwen",
        mem0_base_url="http://mem0.test",
        allow_real_provider=True,
    ) is None


def test_translation_adapter_disables_qwen_native_web_search() -> None:
    adapter = create_memory_translation_adapter(
        ProviderConfig(
            provider_mode="real",
            chat_provider="qwen",
            qwen_api_key="test-key",
        )
    )

    assert adapter.provider == "qwen"
    assert adapter.native_web_search is False

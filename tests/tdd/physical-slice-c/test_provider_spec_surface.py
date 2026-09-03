from assistant_agent.providers.specs import resolved_chat_provider


def test_resolved_provider_contains_only_consumed_configuration_metadata() -> None:
    resolved = resolved_chat_provider(
        "qwen",
        api_key="key",
        base_url="https://example.test/v1",
        model="qwen-test",
    )

    assert resolved.provider == "qwen"
    assert resolved.adapter_kind == "openai_compatible"
    assert resolved.missing_required_env() == []
    assert not hasattr(resolved, "capabilities")
    assert not hasattr(resolved.spec, "capabilities")
    assert not hasattr(resolved.spec, "capability")
    assert not hasattr(resolved.spec, "provider_env")

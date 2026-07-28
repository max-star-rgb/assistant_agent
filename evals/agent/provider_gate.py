"""Fail-closed gate for Agent evals that require a real Chat Provider."""

from __future__ import annotations

from assistant_agent.config import ProviderConfig


def validate_real_chat_config(config: ProviderConfig) -> None:
    if config.provider_mode != "real":
        raise RuntimeError("Agent eval requires MULTIMODAL_AGENT_PROVIDER_MODE=real.")
    if config.chat_provider == "mock" or config.chat_adapter_kind == "mock":
        raise RuntimeError("Agent eval requires a configured real Chat Provider.")
    missing = config.resolved_chat_provider().missing_required_env()
    if missing:
        raise RuntimeError(
            "Agent eval Chat Provider is missing: " + ", ".join(missing) + "."
        )

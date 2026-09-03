"""Fail-closed preflight checks shared by real system evals."""

from assistant_agent.config import ChatConfig
from assistant_agent.provider_mode import ProviderMode


class SystemEvalConfigurationError(RuntimeError):
    """A real system eval is not explicitly or completely configured."""


def validate_real_chat_config(
    config: ChatConfig,
    *,
    provider_mode: ProviderMode,
) -> None:
    """Require real mode and a completely configured non-mock chat Provider."""

    if provider_mode != "real":
        raise SystemEvalConfigurationError(
            "System eval requires MULTIMODAL_AGENT_PROVIDER_MODE=real."
        )
    if config.resolved_provider().adapter_kind == "mock":
        raise SystemEvalConfigurationError(
            "System eval requires an explicit real chat Provider."
        )
    missing = config.resolved_provider().missing_required_env()
    if missing:
        raise SystemEvalConfigurationError(
            "System eval chat Provider is missing: " + ", ".join(missing) + "."
        )

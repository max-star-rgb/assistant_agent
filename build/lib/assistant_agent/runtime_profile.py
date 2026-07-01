"""Runtime profile settings for local demo, eval, smoke, and pilot modes."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


RuntimeProfileName = Literal["local_demo", "offline_eval", "provider_smoke", "pilot"]
ProviderMode = Literal["mock", "explicit"]

PROFILE_ENV_VAR = "MULTIMODAL_AGENT_RUNTIME_PROFILE"
DEFAULT_RUNTIME_PROFILE: RuntimeProfileName = "local_demo"


class RuntimeProfileError(ValueError):
    """Raised when runtime profile configuration is invalid."""


@dataclass(frozen=True)
class RuntimeProfile:
    """Explicit runtime mode and its safety boundaries."""

    name: RuntimeProfileName
    allows_real_providers: bool
    allows_network_provider_calls: bool
    requires_explicit_provider_config: bool
    default_provider_mode: ProviderMode
    description: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeProfile":
        """Load the runtime profile from environment-like data."""

        source = os.environ if env is None else env
        return get_runtime_profile(source.get(PROFILE_ENV_VAR))


RUNTIME_PROFILES: dict[RuntimeProfileName, RuntimeProfile] = {
    "local_demo": RuntimeProfile(
        name="local_demo",
        allows_real_providers=False,
        allows_network_provider_calls=False,
        requires_explicit_provider_config=False,
        default_provider_mode="mock",
        description="Default local CLI/API/Web demo mode using mock/local providers.",
    ),
    "offline_eval": RuntimeProfile(
        name="offline_eval",
        allows_real_providers=False,
        allows_network_provider_calls=False,
        requires_explicit_provider_config=False,
        default_provider_mode="mock",
        description="Deterministic offline evaluation and regression mode.",
    ),
    "provider_smoke": RuntimeProfile(
        name="provider_smoke",
        allows_real_providers=True,
        allows_network_provider_calls=True,
        requires_explicit_provider_config=True,
        default_provider_mode="explicit",
        description="Manual opt-in smoke mode for configured real providers.",
    ),
    "pilot": RuntimeProfile(
        name="pilot",
        allows_real_providers=True,
        allows_network_provider_calls=True,
        requires_explicit_provider_config=True,
        default_provider_mode="explicit",
        description="Controlled real-usage pilot mode with explicit provider configuration.",
    ),
}


def get_runtime_profile(value: str | None = None) -> RuntimeProfile:
    """Return a runtime profile by name, defaulting to local_demo."""

    profile_name = (value or DEFAULT_RUNTIME_PROFILE).strip()
    if profile_name in RUNTIME_PROFILES:
        return RUNTIME_PROFILES[profile_name]  # type: ignore[index]
    allowed = ", ".join(sorted(RUNTIME_PROFILES))
    raise RuntimeProfileError(
        f"Invalid {PROFILE_ENV_VAR}: {profile_name!r}. Expected one of: {allowed}."
    )

"""Global provider execution mode."""

import os
from collections.abc import Mapping
from typing import Literal


ProviderMode = Literal["mock", "real"]
PROVIDER_MODE_ENV_VAR = "MULTIMODAL_AGENT_PROVIDER_MODE"
DEFAULT_PROVIDER_MODE: ProviderMode = "mock"


class ProviderModeError(ValueError):
    """Raised when the global provider mode is invalid."""


def get_provider_mode(env: Mapping[str, str] | None = None) -> ProviderMode:
    """Load the single global mock/real provider mode."""

    source = os.environ if env is None else env
    value = (source.get(PROVIDER_MODE_ENV_VAR) or DEFAULT_PROVIDER_MODE).strip().lower()
    if value in {"mock", "real"}:
        return value  # type: ignore[return-value]
    raise ProviderModeError(
        f"Invalid {PROVIDER_MODE_ENV_VAR}: {value!r}. Expected one of: mock, real."
    )

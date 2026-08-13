"""Optional native LangSmith client configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


ASSISTANT_AGENT_LANGSMITH_ENABLED_ENV = "ASSISTANT_AGENT_LANGSMITH_ENABLED"
LANGSMITH_API_KEY_ENV = "LANGSMITH_API_KEY"
LANGSMITH_ENDPOINT_ENV = "LANGSMITH_ENDPOINT"
LANGSMITH_PROJECT_ENV = "LANGSMITH_PROJECT"
LANGSMITH_WORKSPACE_ID_ENV = "LANGSMITH_WORKSPACE_ID"
DEFAULT_LANGSMITH_ENDPOINT = "https://api.smith.langchain.com"
DEFAULT_LANGSMITH_PROJECT = "assistant-agent-runtime"
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class LangSmithConfig:
    """Environment-backed, independently switchable LangSmith settings."""

    enabled: bool = False
    api_key: str | None = None
    endpoint: str = DEFAULT_LANGSMITH_ENDPOINT
    project: str = DEFAULT_LANGSMITH_PROJECT
    workspace_id: str | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        project_override: str | None = None,
    ) -> "LangSmithConfig":
        values = os.environ if env is None else env
        enabled = _truthy(values.get(ASSISTANT_AGENT_LANGSMITH_ENABLED_ENV))
        api_key = _non_empty(values.get(LANGSMITH_API_KEY_ENV))
        endpoint = (
            _non_empty(values.get(LANGSMITH_ENDPOINT_ENV))
            or DEFAULT_LANGSMITH_ENDPOINT
        )
        project = (
            _non_empty(project_override)
            or _non_empty(values.get(LANGSMITH_PROJECT_ENV))
            or DEFAULT_LANGSMITH_PROJECT
        )
        workspace_id = _non_empty(values.get(LANGSMITH_WORKSPACE_ID_ENV))
        if enabled and api_key is None:
            raise RuntimeError(
                "LANGSMITH_API_KEY is required when LangSmith is enabled"
            )
        return cls(
            enabled=enabled,
            api_key=api_key,
            endpoint=endpoint,
            project=project,
            workspace_id=workspace_id,
        )

def create_langsmith_client_from_env(
    env: Mapping[str, str] | None = None,
) -> Any:
    """Create the optional SDK client only after explicit enablement."""

    config = LangSmithConfig.from_env(env)
    if not config.enabled:
        raise RuntimeError("LangSmith is disabled")
    return create_langsmith_client(config)


def create_langsmith_client(config: LangSmithConfig) -> Any:
    """Create an SDK client for an already validated native tracing config."""

    if not config.enabled:
        raise RuntimeError("LangSmith is disabled")
    from langsmith import Client

    return Client(
        api_key=config.api_key,
        api_url=config.endpoint,
        workspace_id=config.workspace_id,
    )

def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY_VALUES


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None

"""Shared opt-in fixtures for real-provider tool plugin tests."""

from __future__ import annotations

import os

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import load_mcp_server_configs_from_env
from assistant_agent.services.assistant_run_service import load_env_file
from assistant_agent.tools.plugins.contracts import ToolPluginContext


REAL_TOOL_PLUGIN_TEST_ENV = "ASSISTANT_AGENT_RUN_REAL_TOOL_PLUGIN_TESTS"


@pytest.fixture(scope="session")
def real_provider_config() -> ProviderConfig:
    """Load explicit real-provider configuration without affecting default pytest."""

    if os.getenv(REAL_TOOL_PLUGIN_TEST_ENV) != "1":
        pytest.skip(f"set {REAL_TOOL_PLUGIN_TEST_ENV}=1 to run real-provider plugin tests")
    load_env_file()
    config = ProviderConfig.from_env()
    if config.provider_mode != "real":
        pytest.fail("real-provider plugin tests require MULTIMODAL_AGENT_PROVIDER_MODE=real")
    return config


@pytest.fixture(scope="session")
def real_plugin_context(real_provider_config: ProviderConfig) -> ToolPluginContext:
    """Build the shared plugin context from real provider and MCP configuration."""

    return ToolPluginContext(
        config=real_provider_config,
        mcp_server_configs=load_mcp_server_configs_from_env(),
    )

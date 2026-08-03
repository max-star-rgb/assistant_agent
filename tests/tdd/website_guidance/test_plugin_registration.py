from __future__ import annotations

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.website_guidance.backend import (
    MockWebsiteGuidanceBackend,
)
from assistant_agent.tools.plugins.builtin.website_guidance.plugin import (
    WebsiteGuidancePlugin,
)
from assistant_agent.tools.plugins.builtin.website_guidance import plugin as website_guidance_plugin
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.defaults import default_tool_plugins


def _config(*, provider_mode: str, enabled: bool = True, timeout: float = 2.5) -> ProviderConfig:
    if provider_mode == "mock":
        return ProviderConfig(
            provider_mode="mock",
            website_guidance_enabled=enabled,
            website_guidance_navigation_timeout_seconds=timeout,
        )
    return ProviderConfig(
        provider_mode="real",
        chat_provider="qwen",
        chat_api_key="test-key",
        chat_base_url="https://example.invalid/v1",
        chat_model="test-model",
        website_guidance_enabled=enabled,
        website_guidance_navigation_timeout_seconds=timeout,
    )


def _context(config: ProviderConfig) -> ToolPluginContext:
    return ToolPluginContext(config=config, mcp_server_configs=[])


def _tool_names(tools: list[object]) -> list[str]:
    return [tool.name for tool in tools]  # type: ignore[attr-defined]


def test_website_guidance_is_disabled_by_default() -> None:
    assert ProviderConfig().website_guidance_enabled is False


@pytest.mark.parametrize("provider_mode", ["mock", "real"])
def test_disabled_website_guidance_registers_no_tools(provider_mode: str) -> None:
    plugin = WebsiteGuidancePlugin(
        readiness_probe=lambda: (_ for _ in ()).throw(AssertionError("unexpected probe")),
        real_backend_factory=lambda _timeout: (_ for _ in ()).throw(
            AssertionError("unexpected real backend")
        ),
    )

    assert plugin.build_tools(_context(_config(provider_mode=provider_mode, enabled=False))) == []


def test_enabled_mock_registers_two_tools_with_one_offline_backend() -> None:
    backend = MockWebsiteGuidanceBackend()
    plugin = WebsiteGuidancePlugin(
        mock_backend_factory=lambda: backend,
        readiness_probe=lambda: (_ for _ in ()).throw(AssertionError("mock must not probe")),
        real_backend_factory=lambda _timeout: (_ for _ in ()).throw(
            AssertionError("mock must not construct real backend")
        ),
    )

    tools = plugin.build_tools(_context(_config(provider_mode="mock")))

    assert _tool_names(tools) == ["web_page_inspect", "web_page_explore"]
    assert tools[0].backend is backend
    assert tools[1].backend is backend


def test_enabled_real_without_readiness_registers_no_tools() -> None:
    plugin = WebsiteGuidancePlugin(
        readiness_probe=lambda: False,
        real_backend_factory=lambda _timeout: (_ for _ in ()).throw(
            AssertionError("unready browser must not construct backend")
        ),
    )

    assert plugin.build_tools(_context(_config(provider_mode="real"))) == []


def test_enabled_real_with_readiness_registers_two_tools_with_one_real_backend() -> None:
    backend = MockWebsiteGuidanceBackend()
    requested_timeouts: list[float] = []
    plugin = WebsiteGuidancePlugin(
        readiness_probe=lambda: True,
        real_backend_factory=lambda timeout: (
            requested_timeouts.append(timeout) or backend
        ),
    )

    tools = plugin.build_tools(_context(_config(provider_mode="real", timeout=2.5)))

    assert _tool_names(tools) == ["web_page_inspect", "web_page_explore"]
    assert tools[0].backend is backend
    assert tools[1].backend is backend
    assert requested_timeouts == [2.5]


def test_enabled_real_readiness_or_backend_failure_is_fail_closed() -> None:
    probe_failure = WebsiteGuidancePlugin(
        readiness_probe=lambda: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )
    backend_failure = WebsiteGuidancePlugin(
        readiness_probe=lambda: True,
        real_backend_factory=lambda _timeout: (_ for _ in ()).throw(
            RuntimeError("backend failed")
        ),
    )
    context = _context(_config(provider_mode="real"))

    assert probe_failure.build_tools(context) == []
    assert backend_failure.build_tools(context) == []


@pytest.mark.parametrize("provider_mode", ["mock", "real"])
def test_tool_construction_failure_is_fail_closed_without_backend_fallback(
    provider_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingInspectTool:
        def __init__(self, backend: object) -> None:
            raise RuntimeError("tool construction failed")

    unexpected_backend_calls: list[str] = []
    monkeypatch.setattr(website_guidance_plugin, "WebPageInspectTool", FailingInspectTool)
    if provider_mode == "mock":
        plugin = WebsiteGuidancePlugin(
            mock_backend_factory=MockWebsiteGuidanceBackend,
            readiness_probe=lambda: True,
            real_backend_factory=lambda _timeout: (
                unexpected_backend_calls.append("real")
                or MockWebsiteGuidanceBackend()
            ),
        )
    else:
        plugin = WebsiteGuidancePlugin(
            mock_backend_factory=lambda: (
                unexpected_backend_calls.append("mock")
                or MockWebsiteGuidanceBackend()
            ),
            readiness_probe=lambda: True,
            real_backend_factory=lambda _timeout: MockWebsiteGuidanceBackend(),
        )

    assert plugin.build_tools(_context(_config(provider_mode=provider_mode))) == []
    assert unexpected_backend_calls == []


@pytest.mark.parametrize("timeout", ["0", "-1", "30.1", "not-a-number"])
def test_invalid_timeout_from_env_disables_website_guidance(timeout: str) -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_WEBSITE_GUIDANCE_ENABLED": "true",
            "WEBSITE_GUIDANCE_NAVIGATION_TIMEOUT_SECONDS": timeout,
        }
    )

    assert config.website_guidance_enabled is False


def test_invalid_direct_timeout_is_fail_closed_even_when_enabled() -> None:
    plugin = WebsiteGuidancePlugin(
        readiness_probe=lambda: (_ for _ in ()).throw(AssertionError("unexpected probe")),
        real_backend_factory=lambda _timeout: (_ for _ in ()).throw(
            AssertionError("unexpected real backend")
        ),
    )

    assert plugin.build_tools(_context(_config(provider_mode="mock", timeout=0.0))) == []


def test_default_plugin_list_declares_website_guidance_plugin() -> None:
    descriptors = {
        (plugin.descriptor.plugin_id, plugin.descriptor.plugin_version)
        for plugin in default_tool_plugins()
    }

    assert ("website_guidance", "1") in descriptors

"""Explicit, fail-closed registration for read-only website guidance tools."""

from __future__ import annotations

import math
from collections.abc import Callable

from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.builtin.website_guidance.backend import (
    MockWebsiteGuidanceBackend,
    WebsiteGuidanceBackend,
)
from assistant_agent.tools.plugins.builtin.website_guidance.tools import (
    WebPageExploreTool,
    WebPageInspectTool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext, ToolPluginDescriptor


MockBackendFactory = Callable[[], WebsiteGuidanceBackend]
RealBackendFactory = Callable[[float], WebsiteGuidanceBackend]
ReadinessProbe = Callable[[], bool]


class WebsiteGuidancePlugin:
    """Register website guidance only for an explicit, usable configuration."""

    descriptor = ToolPluginDescriptor(plugin_id="website_guidance", plugin_version="1")

    def __init__(
        self,
        *,
        mock_backend_factory: MockBackendFactory | None = None,
        readiness_probe: ReadinessProbe | None = None,
        real_backend_factory: RealBackendFactory | None = None,
    ) -> None:
        self._mock_backend_factory = (
            MockWebsiteGuidanceBackend
            if mock_backend_factory is None
            else mock_backend_factory
        )
        self._readiness_probe = (
            _playwright_browser_ready if readiness_probe is None else readiness_probe
        )
        self._real_backend_factory = (
            _create_playwright_backend
            if real_backend_factory is None
            else real_backend_factory
        )

    def build_tools(self, context: ToolPluginContext) -> list[Tool]:
        config = context.config
        if not _website_guidance_enabled(
            config.website_guidance_enabled,
            config.website_guidance_navigation_timeout_seconds,
        ):
            return []

        if context.mock_mode:
            try:
                backend = self._mock_backend_factory()
                return _tools_for(backend)
            except Exception:
                return []

        try:
            if not self._readiness_probe():
                return []
            backend = self._real_backend_factory(
                config.website_guidance_navigation_timeout_seconds
            )
            return _tools_for(backend)
        except Exception:
            return []


def _tools_for(backend: WebsiteGuidanceBackend) -> list[Tool]:
    """Construct both public projections over one shared backend and store."""

    return [WebPageInspectTool(backend=backend), WebPageExploreTool(backend=backend)]


def _website_guidance_enabled(enabled: object, navigation_timeout_seconds: object) -> bool:
    return (
        enabled is True
        and isinstance(navigation_timeout_seconds, (int, float))
        and not isinstance(navigation_timeout_seconds, bool)
        and math.isfinite(navigation_timeout_seconds)
        and 0.0 < navigation_timeout_seconds <= 30.0
    )


def _playwright_browser_ready() -> bool:
    """Load the optional Playwright dependency only for enabled real mode."""

    from assistant_agent.tools.plugins.builtin.website_guidance.playwright_backend import (
        playwright_browser_ready,
    )

    return playwright_browser_ready()


def _create_playwright_backend(
    navigation_timeout_seconds: float,
) -> WebsiteGuidanceBackend:
    """Construct the real backend after readiness has completed successfully."""

    from assistant_agent.tools.plugins.builtin.website_guidance.playwright_backend import (
        BrowserGuidanceLimits,
        PlaywrightWebsiteGuidanceBackend,
    )

    return PlaywrightWebsiteGuidanceBackend(
        limits=BrowserGuidanceLimits(
            navigation_timeout_ms=max(1, math.ceil(navigation_timeout_seconds * 1_000))
        )
    )

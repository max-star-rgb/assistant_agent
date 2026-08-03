"""Backend boundary and deterministic offline implementation for website guidance."""

from typing import Protocol

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.website_guidance.models import (
    WebPageElement,
    WebPageExploreRequest,
    WebPageGuidanceResult,
    WebPageInspectRequest,
)


MOCK_PAGE_URL = "https://example.com/service"
MOCK_BROWSER_SESSION_ID = "mock-browser-session-1"


class WebsiteGuidanceBackend(Protocol):
    """Synchronous backend contract for safe, reference-based web guidance."""

    def inspect(
        self,
        request: WebPageInspectRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        """Inspect a public page without executing page content as instructions."""

    def explore(
        self,
        request: WebPageExploreRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        """Explore a prior browser session using only normalized element references."""


class MockWebsiteGuidanceBackend:
    """Deterministic offline backend for tests and local development."""

    def inspect(
        self,
        request: WebPageInspectRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        return _mock_result(
            summary=f"已找到与目标“{request.goal}”相关的公开页面入口。",
            content="这是离线 mock 的公开服务页面观察结果。可使用 e1 查看办理入口。",
        )

    def explore(
        self,
        request: WebPageExploreRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        action_summary = {
            "inspect": "已重新查看当前公开页面。",
            "click": f"已按引用 {request.element_ref} 查看公开办理入口。",
            "back": "已返回公开服务页面。",
            "wait": "已等待页面公开内容稳定。",
        }[request.action]
        return _mock_result(
            summary=action_summary,
            content="这是离线 mock 的公开服务页面观察结果。可使用 e1 查看办理入口。",
        )


def _mock_result(*, summary: str, content: str) -> WebPageGuidanceResult:
    return WebPageGuidanceResult(
        outcome="success",
        url=MOCK_PAGE_URL,
        browser_session_id=MOCK_BROWSER_SESSION_ID,
        title="Mock 公开服务页面",
        summary=summary,
        content=content,
        elements=[
            WebPageElement(
                ref="e1",
                role="link",
                name="办理入口",
                href="https://example.com/service/apply",
                safe_action="click",
            )
        ],
        output_ref="mock://website-guidance/service",
    )

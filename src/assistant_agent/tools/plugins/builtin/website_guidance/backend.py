"""Backend boundary and deterministic offline implementation for website guidance."""

from datetime import datetime, timezone
import threading
from typing import Protocol

from assistant_agent.tools.runtime import ToolContext
from assistant_agent.tools.plugins.builtin.website_guidance.models import (
    WebPageElement,
    WebPageExploreRequest,
    WebPageGuidanceError,
    WebPageGuidanceResult,
    WebPageInspectRequest,
)


MOCK_PAGE_URL = "https://example.com/service"
_MOCK_UNAVAILABLE_SESSION_ID = "mock-unavailable-session"


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

    def cleanup_run(self, run_id: str) -> int:
        """Release opaque browser metadata owned by one terminal run."""


class MockWebsiteGuidanceBackend:
    """Deterministic offline backend for tests and local development."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._next_session_number = 1
        self._session_owners: dict[str, tuple[str, str]] = {}

    def inspect(
        self,
        request: WebPageInspectRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        requested_url = str(request.url)
        if context.is_cancelled():
            return _mock_error(
                outcome="failed",
                requested_url=requested_url,
                browser_session_id=_MOCK_UNAVAILABLE_SESSION_ID,
                code="agent_run_cancelled",
            )
        owner = _mock_owner(context)
        if owner is None:
            return _mock_error(
                outcome="failed",
                requested_url=requested_url,
                browser_session_id=_MOCK_UNAVAILABLE_SESSION_ID,
                code="missing_browser_owner",
            )
        if requested_url != MOCK_PAGE_URL:
            return _mock_error(
                requested_url=requested_url,
                browser_session_id=_MOCK_UNAVAILABLE_SESSION_ID,
                code="mock_url_unverified",
            )
        with self._lock:
            browser_session_id = (
                f"mock-browser-session-{self._next_session_number}"
            )
            self._next_session_number += 1
            self._session_owners[browser_session_id] = owner
        return _mock_result(
            requested_url=requested_url,
            browser_session_id=browser_session_id,
            summary=f"已找到与目标“{request.goal}”相关的公开页面入口。",
            content="这是离线 mock 的公开服务页面观察结果。可使用 e1 查看办理入口。",
        )

    def explore(
        self,
        request: WebPageExploreRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        if context.is_cancelled():
            return _mock_error(
                outcome="failed",
                requested_url=MOCK_PAGE_URL,
                browser_session_id=request.browser_session_id,
                code="agent_run_cancelled",
            )
        owner = _mock_owner(context)
        if owner is None:
            return _mock_error(
                outcome="failed",
                requested_url=MOCK_PAGE_URL,
                browser_session_id=request.browser_session_id,
                code="missing_browser_owner",
            )
        with self._lock:
            issued_owner = self._session_owners.get(request.browser_session_id)
        if issued_owner != owner:
            return _mock_error(
                requested_url=MOCK_PAGE_URL,
                browser_session_id=request.browser_session_id,
                code="browser_session_unavailable",
            )
        action_summary = {
            "inspect": "已重新查看当前公开页面。",
            "click": f"已按引用 {request.element_ref} 查看公开办理入口。",
            "back": "已返回公开服务页面。",
            "wait": "已等待页面公开内容稳定。",
        }[request.action]
        return _mock_result(
            requested_url=MOCK_PAGE_URL,
            browser_session_id=request.browser_session_id,
            summary=action_summary,
            content="这是离线 mock 的公开服务页面观察结果。可使用 e1 查看办理入口。",
        )

    def cleanup_run(self, run_id: str) -> int:
        with self._lock:
            session_ids = [
                browser_session_id
                for browser_session_id, owner in self._session_owners.items()
                if owner[0] == run_id
            ]
            for browser_session_id in session_ids:
                del self._session_owners[browser_session_id]
            return len(session_ids)


def _mock_result(
    *,
    requested_url: str,
    browser_session_id: str,
    summary: str,
    content: str,
) -> WebPageGuidanceResult:
    return WebPageGuidanceResult(
        outcome="success",
        url=MOCK_PAGE_URL,
        requested_url=requested_url,
        final_url=MOCK_PAGE_URL,
        checked_at=datetime.now(timezone.utc),
        browser_session_id=browser_session_id,
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


def _mock_error(
    *,
    outcome: str = "blocked",
    requested_url: str,
    browser_session_id: str,
    code: str,
) -> WebPageGuidanceResult:
    return WebPageGuidanceResult(
        outcome=outcome,
        url=requested_url,
        requested_url=requested_url,
        final_url=None,
        checked_at=datetime.now(timezone.utc),
        browser_session_id=browser_session_id,
        errors=[WebPageGuidanceError(code=code, message=code)],
    )


def _mock_owner(context: ToolContext) -> tuple[str, str] | None:
    if not context.run_id or not context.session_id:
        return None
    return context.run_id, context.session_id

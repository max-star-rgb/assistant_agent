"""Synchronous Playwright backend for bounded, read-only website guidance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from assistant_agent.tools.runtime import ToolContext
from assistant_agent.tools.plugins.builtin.website_guidance.models import (
    WebPageElement,
    WebPageExploreRequest,
    WebPageGuidanceError,
    WebPageGuidanceResult,
    WebPageInspectRequest,
)
from assistant_agent.tools.plugins.builtin.website_guidance.session_store import (
    BrowserElementDescriptor,
    BrowserExplorationAction,
    BrowserExplorationStore,
    MAX_SNAPSHOT_ELEMENTS,
)
from assistant_agent.tools.plugins.builtin.website_guidance.url_policy import (
    ValidatedWebTarget,
    WebUrlValidationError,
    validate_public_web_url,
)


_UNAVAILABLE_SESSION_ID = "unavailable-session"
_UNAVAILABLE_URL = "https://unavailable.invalid/"
_SAFE_HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
_READ_ONLY_METHODS = frozenset({"GET", "HEAD"})
_ASYNC_GUARD_DRAIN_MS = 100
_MAX_CANDIDATES_SCANNED = 160
_MAX_REDIRECT_HOPS = 10
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_BOUNDED_INNER_TEXT_SCRIPT = (
    "(element, limit) => (element.innerText || '').slice(0, limit)"
)
_BOUNDED_ELEMENT_NAME_SCRIPT = """(element, limit) => {
    const value = element.getAttribute('aria-label') || element.innerText || '';
    return value.trim().slice(0, limit);
}"""
_EXPAND_NODE_IDENTITY_SCRIPT = r"""(element, limit) => {
    const stableNodeId = (element.getAttribute('id') || '').trim();
    const name = (
        element.getAttribute('aria-label') || element.innerText || ''
    ).trim().replace(/\s+/g, ' ').slice(0, limit);
    let stableNodeIdUnique = false;
    if (stableNodeId && stableNodeId.length <= 256) {
        const escaped = CSS.escape(stableNodeId);
        stableNodeIdUnique = (
            document.getElementById(stableNodeId) === element
            && document.querySelectorAll(`#${escaped}`).length === 1
        );
    }
    return {
        connected: element.isConnected,
        tagName: element.tagName,
        stableNodeId,
        stableNodeIdUnique,
        name,
        type: (element.getAttribute('type') || 'button').trim().toLowerCase(),
        hasFormAttribute: element.hasAttribute('form'),
        hasFormAction: element.hasAttribute('formaction'),
        insideForm: element.closest('form') !== null,
        hasAriaExpanded: element.hasAttribute('aria-expanded'),
    };
}"""
_NETWORK_LOCKDOWN_SCRIPT = """(() => {
    const blocked = class {
        constructor() { throw new DOMException('Blocked', 'SecurityError'); }
    };
    for (const name of ['WebTransport', 'RTCPeerConnection', 'webkitRTCPeerConnection']) {
        if (name in globalThis) {
            Object.defineProperty(globalThis, name, {
                value: blocked,
                configurable: false,
                writable: false,
            });
        }
    }
})();"""


UrlPolicy = Callable[[str], ValidatedWebTarget]
PlaywrightFactory = Callable[[], Any]


@dataclass(frozen=True)
class BrowserGuidanceLimits:
    """Hard limits applied inside the real browser boundary."""

    navigation_timeout_ms: int = 10_000
    wait_timeout_ms: int = 2_000
    max_visible_chars: int = 12_000
    max_elements: int = 40
    max_actions_per_session: int = 6
    session_ttl_seconds: int = 120

    def __post_init__(self) -> None:
        for field_name, value in self.__dict__.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_elements > MAX_SNAPSHOT_ELEMENTS:
            raise ValueError(
                f"max_elements cannot exceed {MAX_SNAPSHOT_ELEMENTS}"
            )


@dataclass
class _RequestGuard:
    origin: tuple[str, str, int]
    url_policy: UrlPolicy
    navigation_allowed: bool = False
    network_silent: bool = False
    violation: str | None = None
    last_navigation_response: Any | None = None

    def handle(self, route: Any) -> None:
        request = route.request
        request_url = str(request.url)
        method = str(request.method).upper()
        is_document = str(request.resource_type) == "document"
        if method not in _READ_ONLY_METHODS:
            self.reject(route, "unsafe_request_method")
            return

        if is_document:
            try:
                _validated_target(request_url, self.url_policy)
            except WebUrlValidationError:
                self.reject(route, "unsafe_navigation")
                return

        try:
            request_origin = _origin(request_url)
        except WebUrlValidationError:
            self.reject(
                route,
                "unsafe_navigation" if is_document else "cross_origin_resource",
            )
            return
        if request_origin != self.origin:
            self.reject(
                route,
                "cross_origin_navigation" if is_document else "cross_origin_resource",
            )
            return
        if is_document and not self.navigation_allowed:
            self.reject(route, "unexpected_navigation")
            return
        if self.network_silent:
            self.reject(route, "network_activity_blocked")
            return
        route.continue_()

    def reject(self, route: Any, code: str) -> None:
        if self.violation is None:
            self.violation = code
        route.abort("blockedbyclient")

    def note(self, code: str) -> None:
        if self.violation is None:
            self.violation = code

    def raise_if_violated(self) -> None:
        if self.violation is not None:
            raise _GuidanceBlocked(self.violation)


@dataclass(frozen=True)
class _SafeElement:
    public: WebPageElement
    kind: str
    href: str | None = None
    node_id: str | None = None
    handle: Any | None = None


@dataclass(frozen=True)
class _PageSnapshot:
    requested_url: str
    url: str
    checked_at: datetime
    outcome: str
    warnings: tuple[str, ...]
    title: str
    content: str
    elements: tuple[_SafeElement, ...]


class _GuidanceBlocked(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        url: str | None = None,
        final_url: str | None = None,
    ) -> None:
        self.code = code
        self.url = url
        self.final_url = final_url
        super().__init__(code)


class _GuidanceFailed(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        url: str | None = None,
        final_url: str | None = None,
        recoverable: bool = False,
    ) -> None:
        self.code = code
        self.url = url
        self.final_url = final_url
        self.recoverable = recoverable
        super().__init__(code)


class _GuidanceCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class _NavigationEvidence:
    requested_url: str
    final_url: str
    checked_at: datetime
    outcome: str = "success"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ExpandNodeIdentity:
    node_id: str
    name: str


class _SnapshotDeadlineExceeded(TimeoutError):
    pass


@dataclass(frozen=True)
class _SnapshotDeadline:
    expires_at: float

    @classmethod
    def start(cls, timeout_ms: int) -> "_SnapshotDeadline":
        return cls(expires_at=time.monotonic() + timeout_ms / 1_000)

    def remaining_ms(self) -> int:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise _SnapshotDeadlineExceeded("snapshot deadline exceeded")
        return max(1, int(remaining * 1_000))

    def check(self) -> None:
        self.remaining_ms()


def playwright_browser_ready() -> bool:
    """Return whether Playwright and its matching Chromium executable are present."""

    playwright = None
    try:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        return Path(playwright.chromium.executable_path).is_file()
    except Exception:
        return False
    finally:
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


class PlaywrightWebsiteGuidanceBackend:
    """Ephemeral, replay-based website guidance using Playwright Chromium."""

    def __init__(
        self,
        *,
        url_policy: UrlPolicy = validate_public_web_url,
        limits: BrowserGuidanceLimits = BrowserGuidanceLimits(),
        store: BrowserExplorationStore | None = None,
        playwright_factory: PlaywrightFactory | None = None,
        timeout_error_types: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        self._url_policy = url_policy
        self._limits = limits
        self._store = store or BrowserExplorationStore(
            ttl_seconds=limits.session_ttl_seconds
        )
        self._playwright_factory = playwright_factory or _default_playwright_factory
        self._timeout_error_types = (
            timeout_error_types
            if timeout_error_types is not None
            else _playwright_timeout_error_types()
        )
        self._lock = threading.RLock()

    def inspect(
        self,
        request: WebPageInspectRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        start_url = str(request.url)
        if context.is_cancelled():
            return _cancelled_result(
                requested_url=start_url,
                browser_session_id=_UNAVAILABLE_SESSION_ID,
            )
        owner = _owner(context)
        if owner is None:
            return _error_result(
                outcome="failed",
                requested_url=start_url,
                final_url=None,
                browser_session_id=_UNAVAILABLE_SESSION_ID,
                code="missing_browser_owner",
            )

        with self._lock:
            try:
                target = _validated_target(start_url, self._url_policy)
                snapshot = self._run_browser(
                    target=target,
                    context=context,
                    runner=lambda page, guard: self._inspect_page(
                        page,
                        guard,
                        start_url,
                        context,
                    ),
                )
                _raise_if_cancelled(context)
                record = self._store.create(
                    run_id=owner[0],
                    session_id=owner[1],
                    start_url=start_url,
                    snapshot_url=snapshot.url,
                    snapshot_version=1,
                    snapshot_elements=_snapshot_descriptors(snapshot),
                )
                if context.is_cancelled():
                    self._store.delete_run(owner[0])
                    raise _GuidanceCancelled("agent_run_cancelled")
                return _success_result(
                    snapshot,
                    browser_session_id=record.browser_session_id,
                    snapshot_version=record.snapshot_version,
                )
            except WebUrlValidationError as error:
                return _error_result(
                    outcome="blocked",
                    requested_url=start_url,
                    final_url=None,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code=error.code,
                )
            except _GuidanceCancelled:
                return _cancelled_result(
                    requested_url=start_url,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                )
            except _GuidanceBlocked as error:
                return _error_result(
                    outcome="blocked",
                    requested_url=start_url,
                    final_url=error.final_url,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code=error.code,
                )
            except _GuidanceFailed as error:
                return _error_result(
                    outcome="failed",
                    requested_url=start_url,
                    final_url=error.final_url,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code=error.code,
                    recoverable=error.recoverable,
                )
            except self._timeout_error_types:
                return _error_result(
                    outcome="failed",
                    requested_url=start_url,
                    final_url=None,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code="page_timeout",
                    recoverable=True,
                )
            except _SnapshotDeadlineExceeded:
                return _error_result(
                    outcome="failed",
                    requested_url=start_url,
                    final_url=None,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code="page_timeout",
                    recoverable=True,
                )
            except ValueError as error:
                code = str(error)
                if code in {
                    "global_session_limit_exceeded",
                    "run_session_limit_exceeded",
                }:
                    return _error_result(
                        outcome="blocked",
                        requested_url=start_url,
                        final_url=snapshot.url if "snapshot" in locals() else None,
                        browser_session_id=_UNAVAILABLE_SESSION_ID,
                        code=code,
                    )
                return _error_result(
                    outcome="failed",
                    requested_url=start_url,
                    final_url=None,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code="browser_error",
                )
            except Exception:
                return _error_result(
                    outcome="failed",
                    requested_url=start_url,
                    final_url=None,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code="browser_error",
                )

    def explore(
        self,
        request: WebPageExploreRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        if context.is_cancelled():
            return _cancelled_result(
                requested_url=_UNAVAILABLE_URL,
                browser_session_id=request.browser_session_id,
            )
        owner = _owner(context)
        if owner is None:
            return _error_result(
                outcome="failed",
                requested_url=_UNAVAILABLE_URL,
                final_url=None,
                browser_session_id=request.browser_session_id,
                code="missing_browser_owner",
            )

        with self._lock:
            record = self._store.get_owned(
                request.browser_session_id,
                run_id=owner[0],
                session_id=owner[1],
            )
            if record is None:
                return _error_result(
                    outcome="blocked",
                    requested_url=_UNAVAILABLE_URL,
                    final_url=None,
                    browser_session_id=request.browser_session_id,
                    code="browser_session_unavailable",
                    recoverable=True,
                )
            if len(record.actions) >= self._limits.max_actions_per_session:
                return _error_result(
                    outcome="blocked",
                    requested_url=record.snapshot_url,
                    final_url=record.snapshot_url,
                    browser_session_id=request.browser_session_id,
                    code="action_limit_exceeded",
                )

            try:
                target = _validated_target(record.start_url, self._url_policy)
                snapshot, selected_element = self._run_browser(
                    target=target,
                    context=context,
                    runner=lambda page, guard: self._replay_and_apply(
                        page,
                        guard,
                        start_url=record.start_url,
                        replay_actions=record.actions,
                        expected_snapshot_url=record.snapshot_url,
                        expected_snapshot_elements=record.snapshot_elements,
                        requested_action=request.action,
                        requested_ref=request.element_ref,
                        context=context,
                    ),
                )
                _raise_if_cancelled(context)
                next_version = record.snapshot_version + 1
                updated = self._store.append_action(
                    request.browser_session_id,
                    run_id=owner[0],
                    session_id=owner[1],
                    action=request.action,
                    element_ref=request.element_ref,
                    snapshot_version=next_version,
                    selected_element=selected_element,
                    snapshot_url=snapshot.url,
                    snapshot_elements=_snapshot_descriptors(snapshot),
                )
                if updated is None:
                    raise _GuidanceBlocked("browser_session_unavailable")
                if context.is_cancelled():
                    self._store.delete_run(owner[0])
                    raise _GuidanceCancelled("agent_run_cancelled")
                return _success_result(
                    snapshot,
                    browser_session_id=request.browser_session_id,
                    snapshot_version=updated.snapshot_version,
                )
            except WebUrlValidationError as error:
                return _error_result(
                    outcome="blocked",
                    requested_url=record.snapshot_url,
                    final_url=None,
                    browser_session_id=request.browser_session_id,
                    code=error.code,
                )
            except _GuidanceCancelled:
                return _cancelled_result(
                    requested_url=record.snapshot_url,
                    browser_session_id=request.browser_session_id,
                )
            except _GuidanceBlocked as error:
                return _error_result(
                    outcome="blocked",
                    requested_url=record.snapshot_url,
                    final_url=error.final_url,
                    browser_session_id=request.browser_session_id,
                    code=error.code,
                    recoverable=error.code == "invalid_element_ref",
                )
            except _GuidanceFailed as error:
                return _error_result(
                    outcome="failed",
                    requested_url=record.snapshot_url,
                    final_url=error.final_url,
                    browser_session_id=request.browser_session_id,
                    code=error.code,
                    recoverable=error.recoverable,
                )
            except self._timeout_error_types:
                return _error_result(
                    outcome="failed",
                    requested_url=record.snapshot_url,
                    final_url=None,
                    browser_session_id=request.browser_session_id,
                    code="page_timeout",
                    recoverable=True,
                )
            except _SnapshotDeadlineExceeded:
                return _error_result(
                    outcome="failed",
                    requested_url=record.snapshot_url,
                    final_url=None,
                    browser_session_id=request.browser_session_id,
                    code="page_timeout",
                    recoverable=True,
                )
            except Exception:
                return _error_result(
                    outcome="failed",
                    requested_url=record.snapshot_url,
                    final_url=None,
                    browser_session_id=request.browser_session_id,
                    code="browser_error",
                )

    def cleanup_run(self, run_id: str) -> int:
        return self._store.delete_run(run_id)

    def _run_browser(
        self,
        *,
        target: ValidatedWebTarget,
        context: ToolContext,
        runner: Callable[[Any, _RequestGuard], Any],
    ) -> Any:
        manager = None
        playwright = None
        browser = None
        browser_context = None
        page = None
        guard = None
        result = None
        try:
            _raise_if_cancelled(context)
            manager = self._playwright_factory()
            playwright = manager.start()
            _raise_if_cancelled(context)
            browser = playwright.chromium.launch(
                headless=True,
                args=_chromium_launch_args(target),
            )
            _raise_if_cancelled(context)
            browser_context = browser.new_context(
                accept_downloads=False,
                service_workers="block",
            )
            guard = _RequestGuard(
                origin=_origin(target.url),
                url_policy=self._url_policy,
            )

            def reject_websocket(_websocket_route: Any) -> None:
                guard.note("websocket_blocked")

            browser_context.add_init_script(script=_NETWORK_LOCKDOWN_SCRIPT)
            browser_context.route("**/*", guard.handle)
            browser_context.route_web_socket("**/*", reject_websocket)
            page = browser_context.new_page()
            if page is None or not _is_browser_page(page):
                raise _GuidanceFailed("invalid_browser_page")
            _raise_if_cancelled(context)

            def reject_download(download: Any) -> None:
                guard.note(
                    "attachment_response_blocked"
                    if guard.navigation_allowed
                    else "download_blocked"
                )
                try:
                    download.cancel()
                except Exception:
                    pass

            def reject_popup(popup: Any) -> None:
                guard.note("new_window_blocked")
                try:
                    popup.close()
                except Exception:
                    pass

            def record_navigation_response(response: Any) -> None:
                request = getattr(response, "request", None)
                is_navigation_request = getattr(
                    request,
                    "is_navigation_request",
                    None,
                )
                try:
                    if callable(is_navigation_request) and is_navigation_request():
                        guard.last_navigation_response = response
                except Exception:
                    return

            page.on("download", reject_download)
            page.on("popup", reject_popup)
            page.on("response", record_navigation_response)
            browser_context.on("page", reject_popup)
            result = runner(page, guard)
            _raise_if_cancelled(context)
        finally:
            _close_quietly(page)
            _close_quietly(browser_context)
            _close_quietly(browser)
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
        if guard is not None:
            guard.raise_if_violated()
        return result

    def _inspect_page(
        self,
        page: Any,
        guard: _RequestGuard,
        start_url: str,
        context: ToolContext,
    ) -> _PageSnapshot:
        navigation = self._goto(page, guard, start_url, context)
        return self._settled_snapshot(page, guard, context, navigation=navigation)

    def _replay_and_apply(
        self,
        page: Any,
        guard: _RequestGuard,
        *,
        start_url: str,
        replay_actions: tuple[BrowserExplorationAction, ...],
        expected_snapshot_url: str,
        expected_snapshot_elements: tuple[BrowserElementDescriptor, ...],
        requested_action: str,
        requested_ref: str | None,
        context: ToolContext,
    ) -> tuple[_PageSnapshot, BrowserElementDescriptor | None]:
        _raise_if_cancelled(context)
        navigation = self._goto(page, guard, start_url, context)
        snapshot = self._settled_snapshot(
            page,
            guard,
            context,
            navigation=navigation,
        )
        for action in replay_actions:
            _raise_if_cancelled(context)
            if action.action == "click":
                _assert_selected_descriptor(
                    snapshot,
                    action.element_ref,
                    action.selected_element,
                )
            snapshot = self._apply_action(
                page,
                guard,
                action=action.action,
                element_ref=action.element_ref,
                snapshot=snapshot,
                context=context,
            )
            _raise_if_cancelled(context)
        if (
            snapshot.url != expected_snapshot_url
            or _snapshot_descriptors(snapshot) != expected_snapshot_elements
        ):
            raise _GuidanceBlocked(
                "stale_page_snapshot",
                url=snapshot.url,
                final_url=snapshot.url,
            )
        selected_element = None
        if requested_action == "click":
            selected_element = _selected_descriptor(snapshot, requested_ref)
        _raise_if_cancelled(context)
        snapshot = self._apply_action(
            page,
            guard,
            action=requested_action,
            element_ref=requested_ref,
            snapshot=snapshot,
            context=context,
        )
        _raise_if_cancelled(context)
        return snapshot, selected_element

    def _settled_snapshot(
        self,
        page: Any,
        guard: _RequestGuard,
        context: ToolContext,
        *,
        navigation: _NavigationEvidence | None = None,
    ) -> _PageSnapshot:
        _raise_if_cancelled(context)
        page.wait_for_timeout(min(self._limits.wait_timeout_ms, _ASYNC_GUARD_DRAIN_MS))
        _raise_if_cancelled(context)
        guard.raise_if_violated()
        current_url = str(page.url)
        self._validate_final_url(current_url, guard.origin)
        evidence = navigation or _NavigationEvidence(
            requested_url=current_url,
            final_url=current_url,
            checked_at=datetime.now(timezone.utc),
        )
        if evidence.final_url != current_url:
            raise _GuidanceFailed(
                "invalid_navigation_response",
                url=current_url,
                final_url=current_url,
            )
        snapshot = self._snapshot(page, guard, evidence, context)
        _raise_if_cancelled(context)
        guard.raise_if_violated()
        return snapshot

    def _apply_action(
        self,
        page: Any,
        guard: _RequestGuard,
        *,
        action: str,
        element_ref: str | None,
        snapshot: _PageSnapshot,
        context: ToolContext,
    ) -> _PageSnapshot:
        _raise_if_cancelled(context)
        if action == "inspect":
            return self._settled_snapshot(page, guard, context)
        if action == "wait":
            page.wait_for_timeout(self._limits.wait_timeout_ms)
            _raise_if_cancelled(context)
            guard.raise_if_violated()
            self._validate_final_url(str(page.url), guard.origin)
            return self._settled_snapshot(page, guard, context)
        if action == "back":
            guard.last_navigation_response = None
            guard.navigation_allowed = True
            try:
                try:
                    _raise_if_cancelled(context)
                    response = page.go_back(
                        wait_until="domcontentloaded",
                        timeout=self._limits.navigation_timeout_ms,
                    )
                    _raise_if_cancelled(context)
                except _GuidanceCancelled:
                    raise
                except Exception:
                    guard.raise_if_violated()
                    if guard.last_navigation_response is not None:
                        self._validate_navigation_response_contract(
                            guard.last_navigation_response,
                            guard=guard,
                        )
                    raise
            finally:
                guard.navigation_allowed = False
            guard.raise_if_violated()
            current_url = str(page.url)
            self._validate_final_url(current_url, guard.origin)
            navigation = (
                self._validate_navigation_response(
                    response,
                    requested_url=current_url,
                    page=page,
                    guard=guard,
                )
                if response is not None
                else None
            )
            return self._settled_snapshot(
                page,
                guard,
                context,
                navigation=navigation,
            )
        if action != "click" or element_ref is None:
            raise _GuidanceBlocked("invalid_browser_action")

        selected = next(
            (element for element in snapshot.elements if element.public.ref == element_ref),
            None,
        )
        if selected is None:
            raise _GuidanceBlocked(
                "invalid_element_ref",
                url=snapshot.url,
                final_url=snapshot.url,
            )
        if selected.kind == "navigate" and selected.href is not None:
            navigation = self._goto(page, guard, selected.href, context)
            return self._settled_snapshot(
                page,
                guard,
                context,
                navigation=navigation,
            )
        if selected.kind != "expand" or selected.handle is None:
            raise _GuidanceBlocked(
                "invalid_element_ref",
                url=snapshot.url,
                final_url=snapshot.url,
            )

        expected_descriptor = _safe_element_descriptor(selected)
        _assert_expand_handle_descriptor(selected.handle, expected_descriptor)
        previous_url = str(page.url)
        guard.navigation_allowed = False
        previous_network_silent = guard.network_silent
        guard.network_silent = True
        try:
            try:
                _raise_if_cancelled(context)
                selected.handle.click(timeout=self._limits.wait_timeout_ms)
                _raise_if_cancelled(context)
            except _GuidanceCancelled:
                raise
            except Exception:
                guard.raise_if_violated()
                _assert_expand_handle_descriptor(
                    selected.handle,
                    expected_descriptor,
                )
                raise
            guard.raise_if_violated()
            if str(page.url) != previous_url:
                self._validate_final_url(str(page.url), guard.origin)
                raise _GuidanceBlocked(
                    "unexpected_navigation",
                    url=str(page.url),
                    final_url=str(page.url),
                )
            self._validate_final_url(str(page.url), guard.origin)
            updated_snapshot = self._settled_snapshot(page, guard, context)
            _assert_expand_handle_descriptor(selected.handle, expected_descriptor)
            _assert_selected_descriptor(
                updated_snapshot,
                element_ref,
                expected_descriptor,
            )
            return updated_snapshot
        finally:
            guard.network_silent = previous_network_silent

    def _goto(
        self,
        page: Any,
        guard: _RequestGuard,
        url: str,
        context: ToolContext,
    ) -> _NavigationEvidence:
        _raise_if_cancelled(context)
        guard.last_navigation_response = None
        guard.navigation_allowed = True
        try:
            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._limits.navigation_timeout_ms,
                )
                _raise_if_cancelled(context)
            except _GuidanceCancelled:
                raise
            except Exception:
                guard.raise_if_violated()
                if guard.last_navigation_response is not None:
                    self._validate_navigation_response_contract(
                        guard.last_navigation_response,
                        guard=guard,
                    )
                raise
        finally:
            guard.navigation_allowed = False
        guard.raise_if_violated()
        self._validate_final_url(str(page.url), guard.origin)
        evidence = self._validate_navigation_response(
            response,
            requested_url=url,
            page=page,
            guard=guard,
        )
        _raise_if_cancelled(context)
        return evidence

    def _validate_navigation_response(
        self,
        response: Any,
        *,
        requested_url: str,
        page: Any,
        guard: _RequestGuard,
    ) -> _NavigationEvidence:
        response_url, redirected = self._validate_navigation_response_contract(
            response,
            guard=guard,
        )
        page_url = str(page.url)
        if response_url != page_url:
            raise _GuidanceFailed(
                "invalid_navigation_response",
                url=page_url,
                final_url=page_url,
            )

        redirected = redirected or requested_url != response_url
        return _NavigationEvidence(
            requested_url=requested_url,
            final_url=response_url,
            checked_at=datetime.now(timezone.utc),
            outcome="partial" if redirected else "success",
            warnings=("redirected_page",) if redirected else (),
        )

    def _validate_navigation_response_contract(
        self,
        response: Any,
        *,
        guard: _RequestGuard,
    ) -> tuple[str, bool]:
        if response is None:
            raise _GuidanceFailed("invalid_navigation_response")
        status = getattr(response, "status", None)
        response_url = getattr(response, "url", None)
        raw_headers = getattr(response, "headers", None)
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or not isinstance(response_url, str)
            or not response_url
            or not isinstance(raw_headers, Mapping)
        ):
            raise _GuidanceFailed("invalid_navigation_response")

        self._validate_final_url(response_url, guard.origin)
        redirected = self._validate_redirect_chain(response, guard.origin)
        headers = {
            str(key).lower(): str(value)
            for key, value in raw_headers.items()
        }
        if status in {401, 403, 407} or "www-authenticate" in headers:
            raise _GuidanceBlocked(
                "authentication_required",
                url=response_url,
                final_url=response_url,
            )
        if status < 200 or status >= 300:
            raise _GuidanceFailed(
                "http_status_error",
                url=response_url,
                final_url=response_url,
                recoverable=status >= 500,
            )
        disposition = headers.get("content-disposition", "").lower()
        if "attachment" in disposition:
            raise _GuidanceBlocked(
                "attachment_response_blocked",
                url=response_url,
                final_url=response_url,
            )
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in _HTML_CONTENT_TYPES:
            raise _GuidanceBlocked(
                "unsupported_page_type",
                url=response_url,
                final_url=response_url,
            )

        return response_url, redirected

    def _validate_redirect_chain(
        self,
        response: Any,
        allowed_origin: tuple[str, str, int],
    ) -> bool:
        request = getattr(response, "request", None)
        if request is None:
            raise _GuidanceFailed("invalid_navigation_response")
        redirected_from = getattr(request, "redirected_from", None)
        seen: set[int] = set()
        hops = 0
        while redirected_from is not None:
            identity = id(redirected_from)
            if identity in seen or hops >= _MAX_REDIRECT_HOPS:
                raise _GuidanceFailed("invalid_redirect_chain")
            seen.add(identity)
            hops += 1
            hop_url = getattr(redirected_from, "url", None)
            if not isinstance(hop_url, str) or not hop_url:
                raise _GuidanceFailed("invalid_redirect_chain")
            self._validate_final_url(hop_url, allowed_origin)
            response_getter = getattr(redirected_from, "response", None)
            hop_response = response_getter() if callable(response_getter) else None
            hop_status = getattr(hop_response, "status", None)
            if (
                not isinstance(hop_status, int)
                or isinstance(hop_status, bool)
                or not 300 <= hop_status < 400
            ):
                raise _GuidanceFailed("invalid_redirect_chain")
            redirected_from = getattr(redirected_from, "redirected_from", None)
        return hops > 0

    def _validate_final_url(
        self,
        url: str,
        allowed_origin: tuple[str, str, int],
    ) -> None:
        try:
            _validated_target(url, self._url_policy)
            if _origin(url) != allowed_origin:
                raise WebUrlValidationError("unsafe_url")
        except WebUrlValidationError as error:
            raise _GuidanceBlocked(
                "unsafe_final_url",
                url=url,
            ) from error

    def _snapshot(
        self,
        page: Any,
        guard: _RequestGuard,
        navigation: _NavigationEvidence,
        context: ToolContext,
    ) -> _PageSnapshot:
        deadline = _SnapshotDeadline.start(self._limits.navigation_timeout_ms)
        _raise_if_cancelled(context)
        guard.raise_if_violated()
        current_url = str(page.url)
        self._validate_final_url(current_url, guard.origin)
        title = str(page.title())[:1_000]
        _raise_if_cancelled(context)
        deadline.check()
        body = page.locator("body").evaluate(
            _BOUNDED_INNER_TEXT_SCRIPT,
            self._limits.max_visible_chars,
            timeout=deadline.remaining_ms(),
        )
        deadline.check()
        _raise_if_cancelled(context)
        content = body if isinstance(body, str) else ""
        elements = self._safe_elements(
            page,
            current_url,
            guard.origin,
            deadline,
            context,
        )
        _raise_if_cancelled(context)
        deadline.check()
        return _PageSnapshot(
            requested_url=navigation.requested_url,
            url=current_url,
            checked_at=navigation.checked_at,
            outcome=navigation.outcome,
            warnings=navigation.warnings,
            title=title,
            content=content,
            elements=tuple(elements),
        )

    def _safe_elements(
        self,
        page: Any,
        current_url: str,
        allowed_origin: tuple[str, str, int],
        deadline: _SnapshotDeadline,
        context: ToolContext,
    ) -> list[_SafeElement]:
        _raise_if_cancelled(context)
        elements: list[_SafeElement] = []
        candidates_scanned = 0
        links = page.locator("a[href]")
        link_count = min(links.count(), _MAX_CANDIDATES_SCANNED)
        _raise_if_cancelled(context)
        deadline.check()
        for index in range(link_count):
            _raise_if_cancelled(context)
            if len(elements) >= self._limits.max_elements:
                return elements
            candidates_scanned += 1
            locator = links.nth(index)
            href = _get_attribute(locator, "href", deadline)
            _raise_if_cancelled(context)
            if href is None:
                continue
            normalized_href = urljoin(current_url, href)
            if len(normalized_href) > 2_000 or not _is_same_http_origin(
                normalized_href,
                allowed_origin,
            ):
                continue
            download = _get_attribute(locator, "download", deadline)
            _raise_if_cancelled(context)
            if download is not None:
                continue
            target = (_get_attribute(locator, "target", deadline) or "").strip().lower()
            _raise_if_cancelled(context)
            if target not in {"", "_self"}:
                continue
            name = _element_name(locator, deadline)
            _raise_if_cancelled(context)
            if not name:
                continue
            public = WebPageElement(
                ref=f"e{len(elements) + 1}",
                role="link",
                name=name,
                href=normalized_href,
                safe_action="click",
            )
            elements.append(
                _SafeElement(public=public, kind="navigate", href=normalized_href)
            )

        buttons = page.locator("button[aria-expanded]")
        button_count = min(
            buttons.count(),
            _MAX_CANDIDATES_SCANNED - candidates_scanned,
        )
        _raise_if_cancelled(context)
        deadline.check()
        for index in range(button_count):
            _raise_if_cancelled(context)
            if len(elements) >= self._limits.max_elements:
                break
            locator = buttons.nth(index)
            identity = _read_expand_node_identity(
                locator,
                deadline=deadline,
            )
            _raise_if_cancelled(context)
            if identity is None:
                continue
            handle = locator.element_handle(timeout=deadline.remaining_ms())
            deadline.check()
            _raise_if_cancelled(context)
            if handle is None:
                continue
            bound_identity = _read_expand_node_identity(handle)
            _raise_if_cancelled(context)
            if bound_identity != identity:
                continue
            public = WebPageElement(
                ref=f"e{len(elements) + 1}",
                role="button",
                name=identity.name,
                href=None,
                safe_action="click",
            )
            elements.append(
                _SafeElement(
                    public=public,
                    kind="expand",
                    node_id=identity.node_id,
                    handle=handle,
                )
            )
        return elements


def _owner(context: ToolContext) -> tuple[str, str] | None:
    if not context.run_id or not context.session_id:
        return None
    return context.run_id, context.session_id


def _default_playwright_factory() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _playwright_timeout_error_types() -> tuple[type[BaseException], ...]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        return (PlaywrightTimeoutError,)
    except Exception:
        return ()


def _validated_target(url: str, url_policy: UrlPolicy) -> ValidatedWebTarget:
    try:
        target = url_policy(url)
    except WebUrlValidationError:
        raise
    except Exception as error:
        raise WebUrlValidationError("unsafe_url") from error
    parsed = urlsplit(url)
    host = parsed.hostname
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as error:
        raise WebUrlValidationError("unsafe_url") from error
    if (
        target.url != url
        or host is None
        or target.host.rstrip(".").lower() != host.rstrip(".").lower()
        or target.port != port
        or not target.resolved_addresses
    ):
        raise WebUrlValidationError("unsafe_url")
    for address in target.resolved_addresses:
        try:
            ipaddress.ip_address(address)
        except ValueError as error:
            raise WebUrlValidationError("unsafe_resolved_address") from error
    return target


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme.lower() not in {"http", "https"} or host is None:
            raise WebUrlValidationError("unsafe_url")
        ascii_host = host.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (UnicodeError, ValueError) as error:
        raise WebUrlValidationError("unsafe_url") from error
    return parsed.scheme.lower(), ascii_host, port


def _is_same_http_origin(
    url: str,
    allowed_origin: tuple[str, str, int],
) -> bool:
    try:
        return _origin(url) == allowed_origin
    except WebUrlValidationError:
        return False


def _chromium_launch_args(target: ValidatedWebTarget) -> list[str]:
    try:
        host = target.host.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as error:
        raise WebUrlValidationError("unsafe_url") from error
    if not _SAFE_HOST_PATTERN.fullmatch(host):
        try:
            ipaddress.ip_address(host)
        except ValueError as error:
            raise WebUrlValidationError("unsafe_url") from error
    address = target.resolved_addresses[0]
    parsed_address = ipaddress.ip_address(address)
    mapped_address = f"[{address}]" if parsed_address.version == 6 else address
    return [
        "--disable-background-networking",
        "--disable-quic",
        "--disable-sync",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--metrics-recording-only",
        f"--host-resolver-rules=MAP {host} {mapped_address},MAP * ~NOTFOUND",
    ]


def _get_attribute(
    locator: Any,
    name: str,
    deadline: _SnapshotDeadline,
) -> str | None:
    value = locator.get_attribute(name, timeout=deadline.remaining_ms())
    deadline.check()
    return value


def _element_name(locator: Any, deadline: _SnapshotDeadline) -> str:
    raw_name = locator.evaluate(
        _BOUNDED_ELEMENT_NAME_SCRIPT,
        1_000,
        timeout=deadline.remaining_ms(),
    )
    deadline.check()
    if not isinstance(raw_name, str):
        return ""
    return " ".join(raw_name.split())[:1_000]


def _read_expand_node_identity(
    target: Any,
    *,
    deadline: _SnapshotDeadline | None = None,
) -> _ExpandNodeIdentity | None:
    kwargs: dict[str, int] = {}
    if deadline is not None:
        kwargs["timeout"] = deadline.remaining_ms()
    raw = target.evaluate(
        _EXPAND_NODE_IDENTITY_SCRIPT,
        1_000,
        **kwargs,
    )
    if deadline is not None:
        deadline.check()
    if not isinstance(raw, Mapping):
        return None
    node_id = raw.get("stableNodeId")
    name = raw.get("name")
    element_type = raw.get("type")
    if (
        raw.get("connected") is not True
        or raw.get("tagName") != "BUTTON"
        or raw.get("stableNodeIdUnique") is not True
        or raw.get("hasAriaExpanded") is not True
        or raw.get("hasFormAttribute") is not False
        or raw.get("hasFormAction") is not False
        or raw.get("insideForm") is not False
        or element_type not in {"button"}
        or not isinstance(node_id, str)
        or not node_id
        or len(node_id) > 256
        or not isinstance(name, str)
        or not name
        or len(name) > 1_000
    ):
        return None
    return _ExpandNodeIdentity(node_id=node_id, name=" ".join(name.split()))


def _assert_expand_handle_descriptor(
    handle: Any,
    expected: BrowserElementDescriptor,
) -> None:
    try:
        identity = _read_expand_node_identity(handle)
    except Exception as error:
        raise _GuidanceBlocked("stale_page_snapshot") from error
    if identity is None:
        raise _GuidanceBlocked("stale_page_snapshot")
    actual = BrowserElementDescriptor(
        ref=expected.ref,
        kind="expand",
        role="button",
        name=identity.name,
        node_id=identity.node_id,
    )
    if actual != expected:
        raise _GuidanceBlocked("stale_page_snapshot")


def _raise_if_cancelled(context: ToolContext) -> None:
    if context.is_cancelled():
        raise _GuidanceCancelled("agent_run_cancelled")


def _is_browser_page(page: Any) -> bool:
    required_methods = (
        "on",
        "goto",
        "go_back",
        "wait_for_timeout",
        "title",
        "locator",
        "close",
    )
    return hasattr(page, "url") and all(
        callable(getattr(page, name, None)) for name in required_methods
    )


def _snapshot_descriptors(
    snapshot: _PageSnapshot,
) -> tuple[BrowserElementDescriptor, ...]:
    return tuple(_safe_element_descriptor(element) for element in snapshot.elements)


def _safe_element_descriptor(element: _SafeElement) -> BrowserElementDescriptor:
    return BrowserElementDescriptor(
        ref=element.public.ref,
        kind=element.kind,
        role=element.public.role,
        name=element.public.name,
        href=element.href,
        node_id=element.node_id,
    )


def _selected_descriptor(
    snapshot: _PageSnapshot,
    element_ref: str | None,
) -> BrowserElementDescriptor:
    selected = next(
        (
            element
            for element in snapshot.elements
            if element.public.ref == element_ref
        ),
        None,
    )
    if selected is None:
        raise _GuidanceBlocked(
            "invalid_element_ref",
            url=snapshot.url,
            final_url=snapshot.url,
        )
    return _safe_element_descriptor(selected)


def _assert_selected_descriptor(
    snapshot: _PageSnapshot,
    element_ref: str | None,
    expected: BrowserElementDescriptor | None,
) -> None:
    try:
        actual = _selected_descriptor(snapshot, element_ref)
    except _GuidanceBlocked as error:
        raise _GuidanceBlocked(
            "stale_page_snapshot",
            url=snapshot.url,
            final_url=snapshot.url,
        ) from error
    if expected is None or actual != expected:
        raise _GuidanceBlocked(
            "stale_page_snapshot",
            url=snapshot.url,
            final_url=snapshot.url,
        )


def _close_quietly(resource: Any | None) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except Exception:
        pass


def _success_result(
    snapshot: _PageSnapshot,
    *,
    browser_session_id: str,
    snapshot_version: int,
) -> WebPageGuidanceResult:
    return WebPageGuidanceResult(
        outcome=snapshot.outcome,
        url=snapshot.url,
        requested_url=snapshot.requested_url,
        final_url=snapshot.url,
        checked_at=snapshot.checked_at,
        browser_session_id=browser_session_id,
        title=snapshot.title,
        summary="已完成有界、只读的网页观察。",
        content=snapshot.content,
        elements=[element.public for element in snapshot.elements],
        warnings=list(snapshot.warnings),
        output_ref=f"browser://{browser_session_id}/{snapshot_version}",
    )


def _error_result(
    *,
    outcome: str,
    requested_url: str,
    final_url: str | None,
    browser_session_id: str,
    code: str,
    recoverable: bool = False,
) -> WebPageGuidanceResult:
    effective_url = final_url or requested_url
    return WebPageGuidanceResult(
        outcome=outcome,
        url=effective_url,
        requested_url=requested_url,
        final_url=final_url,
        checked_at=datetime.now(timezone.utc),
        browser_session_id=browser_session_id,
        errors=[
            WebPageGuidanceError(
                code=code,
                message=code,
                recoverable=recoverable,
            )
        ],
    )


def _cancelled_result(
    *,
    requested_url: str,
    browser_session_id: str,
) -> WebPageGuidanceResult:
    return _error_result(
        outcome="failed",
        requested_url=requested_url,
        final_url=None,
        browser_session_id=browser_session_id,
        code="agent_run_cancelled",
        recoverable=False,
    )

"""Synchronous Playwright backend for bounded, read-only website guidance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import urljoin, urlsplit

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.website_guidance.models import (
    WebPageElement,
    WebPageExploreRequest,
    WebPageGuidanceError,
    WebPageGuidanceResult,
    WebPageInspectRequest,
)
from assistant_agent.tools.plugins.builtin.website_guidance.session_store import (
    BrowserExplorationAction,
    BrowserExplorationStore,
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


@dataclass
class _RequestGuard:
    origin: tuple[str, str, int]
    url_policy: UrlPolicy
    navigation_allowed: bool = False
    violation: str | None = None

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
    locator: Any | None = None


@dataclass(frozen=True)
class _PageSnapshot:
    url: str
    title: str
    content: str
    elements: tuple[_SafeElement, ...]


class _GuidanceBlocked(RuntimeError):
    def __init__(self, code: str, *, url: str | None = None) -> None:
        self.code = code
        self.url = url
        super().__init__(code)


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
        owner = _owner(context)
        if owner is None:
            return _error_result(
                outcome="failed",
                url=start_url,
                browser_session_id=_UNAVAILABLE_SESSION_ID,
                code="missing_browser_owner",
            )

        with self._lock:
            try:
                target = _validated_target(start_url, self._url_policy)
                snapshot = self._run_browser(
                    target=target,
                    runner=lambda page, guard: self._inspect_page(
                        page,
                        guard,
                        start_url,
                    ),
                )
                record = self._store.create(
                    run_id=owner[0],
                    session_id=owner[1],
                    start_url=start_url,
                    snapshot_version=1,
                )
                return _success_result(
                    snapshot,
                    browser_session_id=record.browser_session_id,
                    snapshot_version=record.snapshot_version,
                )
            except WebUrlValidationError as error:
                return _error_result(
                    outcome="blocked",
                    url=start_url,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code=error.code,
                )
            except _GuidanceBlocked as error:
                return _error_result(
                    outcome="blocked",
                    url=error.url or start_url,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code=error.code,
                )
            except self._timeout_error_types:
                return _error_result(
                    outcome="failed",
                    url=start_url,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code="page_timeout",
                    recoverable=True,
                )
            except Exception:
                return _error_result(
                    outcome="failed",
                    url=start_url,
                    browser_session_id=_UNAVAILABLE_SESSION_ID,
                    code="browser_error",
                )

    def explore(
        self,
        request: WebPageExploreRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        owner = _owner(context)
        if owner is None:
            return _error_result(
                outcome="failed",
                url=_UNAVAILABLE_URL,
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
                    url=_UNAVAILABLE_URL,
                    browser_session_id=request.browser_session_id,
                    code="browser_session_unavailable",
                    recoverable=True,
                )
            if len(record.actions) >= self._limits.max_actions_per_session:
                return _error_result(
                    outcome="blocked",
                    url=record.start_url,
                    browser_session_id=request.browser_session_id,
                    code="action_limit_exceeded",
                )

            try:
                target = _validated_target(record.start_url, self._url_policy)
                snapshot = self._run_browser(
                    target=target,
                    runner=lambda page, guard: self._replay_and_apply(
                        page,
                        guard,
                        start_url=record.start_url,
                        replay_actions=record.actions,
                        requested_action=request.action,
                        requested_ref=request.element_ref,
                    ),
                )
                next_version = record.snapshot_version + 1
                updated = self._store.append_action(
                    request.browser_session_id,
                    run_id=owner[0],
                    session_id=owner[1],
                    action=request.action,
                    element_ref=request.element_ref,
                    snapshot_version=next_version,
                )
                if updated is None:
                    raise _GuidanceBlocked("browser_session_unavailable")
                return _success_result(
                    snapshot,
                    browser_session_id=request.browser_session_id,
                    snapshot_version=updated.snapshot_version,
                )
            except WebUrlValidationError as error:
                return _error_result(
                    outcome="blocked",
                    url=record.start_url,
                    browser_session_id=request.browser_session_id,
                    code=error.code,
                )
            except _GuidanceBlocked as error:
                return _error_result(
                    outcome="blocked",
                    url=error.url or record.start_url,
                    browser_session_id=request.browser_session_id,
                    code=error.code,
                    recoverable=error.code == "invalid_element_ref",
                )
            except self._timeout_error_types:
                return _error_result(
                    outcome="failed",
                    url=record.start_url,
                    browser_session_id=request.browser_session_id,
                    code="page_timeout",
                    recoverable=True,
                )
            except Exception:
                return _error_result(
                    outcome="failed",
                    url=record.start_url,
                    browser_session_id=request.browser_session_id,
                    code="browser_error",
                )

    def _run_browser(
        self,
        *,
        target: ValidatedWebTarget,
        runner: Callable[[Any, _RequestGuard], _PageSnapshot],
    ) -> _PageSnapshot:
        manager = None
        playwright = None
        browser = None
        browser_context = None
        page = None
        try:
            manager = self._playwright_factory()
            playwright = manager.start()
            browser = playwright.chromium.launch(
                headless=True,
                args=_chromium_launch_args(target),
            )
            browser_context = browser.new_context(
                accept_downloads=False,
                service_workers="block",
            )
            guard = _RequestGuard(
                origin=_origin(target.url),
                url_policy=self._url_policy,
            )
            browser_context.route("**/*", guard.handle)
            page = browser_context.new_page()

            def reject_download(download: Any) -> None:
                guard.note("download_blocked")
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

            page.on("download", reject_download)
            page.on("popup", reject_popup)
            browser_context.on("page", reject_popup)
            return runner(page, guard)
        finally:
            _close_quietly(page)
            _close_quietly(browser_context)
            _close_quietly(browser)
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def _inspect_page(
        self,
        page: Any,
        guard: _RequestGuard,
        start_url: str,
    ) -> _PageSnapshot:
        self._goto(page, guard, start_url)
        return self._snapshot(page, guard)

    def _replay_and_apply(
        self,
        page: Any,
        guard: _RequestGuard,
        *,
        start_url: str,
        replay_actions: tuple[BrowserExplorationAction, ...],
        requested_action: str,
        requested_ref: str | None,
    ) -> _PageSnapshot:
        self._goto(page, guard, start_url)
        snapshot = self._snapshot(page, guard)
        for action in replay_actions:
            self._apply_action(
                page,
                guard,
                action=action.action,
                element_ref=action.element_ref,
                snapshot=snapshot,
            )
            snapshot = self._snapshot(page, guard)
        self._apply_action(
            page,
            guard,
            action=requested_action,
            element_ref=requested_ref,
            snapshot=snapshot,
        )
        return self._snapshot(page, guard)

    def _apply_action(
        self,
        page: Any,
        guard: _RequestGuard,
        *,
        action: str,
        element_ref: str | None,
        snapshot: _PageSnapshot,
    ) -> None:
        if action == "inspect":
            return
        if action == "wait":
            page.wait_for_timeout(self._limits.wait_timeout_ms)
            guard.raise_if_violated()
            self._validate_final_url(str(page.url), guard.origin)
            return
        if action == "back":
            guard.navigation_allowed = True
            try:
                try:
                    page.go_back(
                        wait_until="domcontentloaded",
                        timeout=self._limits.navigation_timeout_ms,
                    )
                except Exception:
                    guard.raise_if_violated()
                    raise
            finally:
                guard.navigation_allowed = False
            guard.raise_if_violated()
            self._validate_final_url(str(page.url), guard.origin)
            return
        if action != "click" or element_ref is None:
            raise _GuidanceBlocked("invalid_browser_action")

        selected = next(
            (element for element in snapshot.elements if element.public.ref == element_ref),
            None,
        )
        if selected is None:
            raise _GuidanceBlocked("invalid_element_ref", url=snapshot.url)
        if selected.kind == "navigate" and selected.href is not None:
            self._goto(page, guard, selected.href)
            return
        if selected.kind != "expand" or selected.locator is None:
            raise _GuidanceBlocked("invalid_element_ref", url=snapshot.url)

        previous_url = str(page.url)
        guard.navigation_allowed = False
        try:
            selected.locator.click(timeout=self._limits.wait_timeout_ms)
        except Exception:
            guard.raise_if_violated()
            raise
        guard.raise_if_violated()
        if str(page.url) != previous_url:
            raise _GuidanceBlocked("unexpected_navigation", url=str(page.url))
        self._validate_final_url(str(page.url), guard.origin)

    def _goto(self, page: Any, guard: _RequestGuard, url: str) -> None:
        guard.navigation_allowed = True
        try:
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._limits.navigation_timeout_ms,
                )
            except Exception:
                guard.raise_if_violated()
                raise
        finally:
            guard.navigation_allowed = False
        guard.raise_if_violated()
        self._validate_final_url(str(page.url), guard.origin)

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
            raise _GuidanceBlocked("unsafe_final_url", url=url) from error

    def _snapshot(self, page: Any, guard: _RequestGuard) -> _PageSnapshot:
        guard.raise_if_violated()
        current_url = str(page.url)
        self._validate_final_url(current_url, guard.origin)
        title = str(page.title())[:1_000]
        body = page.locator("body").inner_text(timeout=self._limits.wait_timeout_ms)
        content = str(body)[: self._limits.max_visible_chars]
        elements = self._safe_elements(page, current_url, guard.origin)
        return _PageSnapshot(
            url=current_url,
            title=title,
            content=content,
            elements=tuple(elements),
        )

    def _safe_elements(
        self,
        page: Any,
        current_url: str,
        allowed_origin: tuple[str, str, int],
    ) -> list[_SafeElement]:
        elements: list[_SafeElement] = []
        links = page.locator("a[href]")
        for index in range(links.count()):
            if len(elements) >= self._limits.max_elements:
                return elements
            locator = links.nth(index)
            href = locator.get_attribute("href")
            if href is None:
                continue
            normalized_href = urljoin(current_url, href)
            if len(normalized_href) > 2_000 or not _is_same_http_origin(
                normalized_href,
                allowed_origin,
            ):
                continue
            if locator.get_attribute("download") is not None:
                continue
            target = (locator.get_attribute("target") or "").strip().lower()
            if target not in {"", "_self"}:
                continue
            name = _element_name(locator, self._limits.wait_timeout_ms)
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
        for index in range(buttons.count()):
            if len(elements) >= self._limits.max_elements:
                break
            locator = buttons.nth(index)
            button_type = (locator.get_attribute("type") or "button").strip().lower()
            if button_type in {"submit", "image"}:
                continue
            if locator.get_attribute("form") is not None:
                continue
            if locator.get_attribute("formaction") is not None:
                continue
            if locator.locator("xpath=ancestor::form").count() != 0:
                continue
            name = _element_name(locator, self._limits.wait_timeout_ms)
            if not name:
                continue
            public = WebPageElement(
                ref=f"e{len(elements) + 1}",
                role="button",
                name=name,
                href=None,
                safe_action="click",
            )
            elements.append(_SafeElement(public=public, kind="expand", locator=locator))
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
        "--disable-sync",
        "--metrics-recording-only",
        f"--host-resolver-rules=MAP {host} {mapped_address},MAP * ~NOTFOUND",
    ]


def _element_name(locator: Any, timeout_ms: int) -> str:
    aria_label = (locator.get_attribute("aria-label") or "").strip()
    raw_name = aria_label or str(locator.inner_text(timeout=timeout_ms)).strip()
    return " ".join(raw_name.split())[:1_000]


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
        outcome="success",
        url=snapshot.url,
        browser_session_id=browser_session_id,
        title=snapshot.title,
        summary="已完成有界、只读的网页观察。",
        content=snapshot.content,
        elements=[element.public for element in snapshot.elements],
        output_ref=f"browser://{browser_session_id}/{snapshot_version}",
    )


def _error_result(
    *,
    outcome: str,
    url: str,
    browser_session_id: str,
    code: str,
    recoverable: bool = False,
) -> WebPageGuidanceResult:
    return WebPageGuidanceResult(
        outcome=outcome,
        url=url,
        browser_session_id=browser_session_id,
        errors=[
            WebPageGuidanceError(
                code=code,
                message=code,
                recoverable=recoverable,
            )
        ],
    )

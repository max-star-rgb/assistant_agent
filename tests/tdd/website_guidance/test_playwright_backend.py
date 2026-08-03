from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import pytest

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.website_guidance.models import (
    WebPageExploreRequest,
    WebPageInspectRequest,
)
from assistant_agent.tools.plugins.builtin.website_guidance.playwright_backend import (
    BrowserGuidanceLimits,
    PlaywrightWebsiteGuidanceBackend,
    playwright_browser_ready,
)
from assistant_agent.tools.plugins.builtin.website_guidance.url_policy import (
    ValidatedWebTarget,
    WebUrlValidationError,
)


PUBLIC_IP = "93.184.216.34"
START_URL = "https://public.example/start"
NEXT_URL = "https://public.example/next"


def _context() -> ToolContext:
    return ToolContext(run_id="run-a", session_id="session-a")


def _target(url: str, *, address: str = PUBLIC_IP) -> ValidatedWebTarget:
    parsed = urlsplit(url)
    return ValidatedWebTarget(
        url=url,
        host=parsed.hostname or "",
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
        resolved_addresses=(address,),
    )


class _RecordingPolicy:
    def __init__(self, rejected: set[str] | None = None) -> None:
        self.rejected = rejected or set()
        self.calls: list[str] = []

    def __call__(self, url: str) -> ValidatedWebTarget:
        self.calls.append(url)
        if url in self.rejected:
            raise WebUrlValidationError("unsafe_url")
        return _target(url)


@dataclass
class _Element:
    tag: str
    text: str
    attrs: dict[str, str] = field(default_factory=dict)
    inside_form: bool = False
    click_effect: str | None = None


@dataclass
class _PageState:
    title: str
    body: str
    elements: list[_Element] = field(default_factory=list)
    final_url: str | None = None
    timeout: bool = False


class _FakeTimeoutError(Exception):
    pass


class _FakeRequest:
    def __init__(self, url: str, *, method: str = "GET", resource_type: str = "document") -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type

    def is_navigation_request(self) -> bool:
        return self.resource_type == "document"


class _FakeRoute:
    def __init__(self, request: _FakeRequest) -> None:
        self.request = request
        self.aborted = False

    def abort(self, *_args: object, **_kwargs: object) -> None:
        self.aborted = True

    def continue_(self) -> None:
        pass


class _FakeLocator:
    def __init__(
        self,
        page: "_FakePage",
        selector: str,
        elements: list[_Element],
    ) -> None:
        self.page = page
        self.selector = selector
        self.elements = elements

    def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> "_FakeLocator":
        return _FakeLocator(self.page, self.selector, [self.elements[index]])

    def get_attribute(self, name: str) -> str | None:
        return self.elements[0].attrs.get(name)

    def inner_text(self, **_kwargs: object) -> str:
        if self.selector == "body":
            return self.page.state.body
        return self.elements[0].text

    def locator(self, selector: str) -> "_FakeLocator":
        self.page.driver.selectors.append(selector)
        if selector != "xpath=ancestor::form":
            raise AssertionError(f"unexpected nested selector: {selector}")
        forms = [_Element("form", "")] if self.elements[0].inside_form else []
        return _FakeLocator(self.page, selector, forms)

    def click(self, **_kwargs: object) -> None:
        self.page.apply_click(self.elements[0])


class _FakePage:
    def __init__(self, context: "_FakeBrowserContext", driver: "_FakeDriver") -> None:
        self.context = context
        self.driver = driver
        self.url = "about:blank"
        self.state = _PageState(title="", body="")
        self.closed = False
        self.goto_urls: list[str] = []
        self.history: list[str] = []
        self._events: dict[str, list[Callable[[object], None]]] = {}

    def on(self, event: str, callback: Callable[[object], None]) -> None:
        self._events.setdefault(event, []).append(callback)

    def goto(self, url: str, **_kwargs: object) -> None:
        self.goto_urls.append(url)
        state = self.driver.states[url]
        if state.timeout:
            raise _FakeTimeoutError("navigation timeout")
        if not self.context.dispatch(url):
            raise RuntimeError("request aborted")
        if self.url != "about:blank":
            self.history.append(self.url)
        self.url = state.final_url or url
        self.state = state

    def go_back(self, **_kwargs: object) -> None:
        if not self.history:
            return
        self.goto(self.history.pop())

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.driver.waits.append(milliseconds)

    def title(self) -> str:
        return self.state.title

    def locator(self, selector: str) -> _FakeLocator:
        self.driver.selectors.append(selector)
        if selector == "body":
            return _FakeLocator(self, selector, [])
        if selector == "a[href]":
            elements = [item for item in self.state.elements if item.tag == "a"]
            return _FakeLocator(self, selector, elements)
        if selector == "button[aria-expanded]":
            elements = [
                item
                for item in self.state.elements
                if item.tag == "button" and "aria-expanded" in item.attrs
            ]
            return _FakeLocator(self, selector, elements)
        raise AssertionError(f"unexpected selector: {selector}")

    def apply_click(self, element: _Element) -> None:
        effect = element.click_effect
        if effect == "download":
            for callback in self._events.get("download", []):
                callback(_FakeDownload())
        elif effect == "popup":
            self.context.emit_page()
        elif effect == "submit":
            self.context.dispatch(self.url, method="POST", resource_type="document")
        elif effect == "cross_origin":
            self.context.dispatch("https://other.example/next")
        elif effect == "same_origin_navigation":
            self.context.dispatch(NEXT_URL)
            self.url = NEXT_URL
            self.state = self.driver.states[NEXT_URL]
        elif effect == "expand":
            self.state = _PageState(
                title=self.state.title,
                body=self.state.body + " expanded",
                elements=self.state.elements,
            )

    def close(self) -> None:
        self.closed = True


class _FakeDownload:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeBrowserContext:
    def __init__(self, browser: "_FakeBrowser", driver: "_FakeDriver", kwargs: dict[str, object]) -> None:
        self.browser = browser
        self.driver = driver
        self.kwargs = kwargs
        self.closed = False
        self.pages: list[_FakePage] = []
        self._route_handler: Callable[[_FakeRoute], None] | None = None
        self._events: dict[str, list[Callable[[object], None]]] = {}

    def route(self, pattern: str, handler: Callable[[_FakeRoute], None]) -> None:
        assert pattern == "**/*"
        self._route_handler = handler

    def on(self, event: str, callback: Callable[[object], None]) -> None:
        self._events.setdefault(event, []).append(callback)

    def new_page(self) -> _FakePage:
        page = _FakePage(self, self.driver)
        self.pages.append(page)
        return page

    def dispatch(self, url: str, *, method: str = "GET", resource_type: str = "document") -> bool:
        route = _FakeRoute(_FakeRequest(url, method=method, resource_type=resource_type))
        assert self._route_handler is not None
        self._route_handler(route)
        return not route.aborted

    def emit_page(self) -> None:
        popup = _FakePage(self, self.driver)
        self.pages.append(popup)
        for callback in self._events.get("page", []):
            callback(popup)

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, driver: "_FakeDriver", launch_kwargs: dict[str, object]) -> None:
        self.driver = driver
        self.launch_kwargs = launch_kwargs
        self.closed = False
        self.contexts: list[_FakeBrowserContext] = []

    def new_context(self, **kwargs: object) -> _FakeBrowserContext:
        context = _FakeBrowserContext(self, self.driver, kwargs)
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, driver: "_FakeDriver") -> None:
        self.driver = driver

    def launch(self, **kwargs: object) -> _FakeBrowser:
        browser = _FakeBrowser(self.driver, kwargs)
        self.driver.browsers.append(browser)
        return browser


class _FakePlaywright:
    def __init__(self, driver: "_FakeDriver") -> None:
        self.chromium = _FakeChromium(driver)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _FakePlaywrightManager:
    def __init__(self, driver: "_FakeDriver") -> None:
        self.driver = driver
        self.playwright = _FakePlaywright(driver)

    def start(self) -> _FakePlaywright:
        self.driver.playwrights.append(self.playwright)
        return self.playwright


class _FakeDriver:
    def __init__(self, states: dict[str, _PageState]) -> None:
        self.states = states
        self.browsers: list[_FakeBrowser] = []
        self.playwrights: list[_FakePlaywright] = []
        self.selectors: list[str] = []
        self.waits: list[int] = []

    def factory(self) -> _FakePlaywrightManager:
        return _FakePlaywrightManager(self)


def _backend(
    driver: _FakeDriver,
    *,
    policy: Callable[[str], ValidatedWebTarget] | None = None,
    limits: BrowserGuidanceLimits | None = None,
) -> PlaywrightWebsiteGuidanceBackend:
    return PlaywrightWebsiteGuidanceBackend(
        url_policy=policy or _RecordingPolicy(),
        limits=limits or BrowserGuidanceLimits(),
        playwright_factory=driver.factory,
        timeout_error_types=(_FakeTimeoutError,),
    )


def _assert_all_resources_closed(driver: _FakeDriver) -> None:
    assert driver.playwrights
    assert all(playwright.stopped for playwright in driver.playwrights)
    assert all(browser.closed for browser in driver.browsers)
    assert all(context.closed for browser in driver.browsers for context in browser.contexts)
    assert all(
        page.closed
        for browser in driver.browsers
        for context in browser.contexts
        for page in context.pages
    )


def test_inspect_uses_fixed_browser_controls_bounds_output_and_closes_resources() -> None:
    elements = [
        _Element("a", f"Link {index}", {"href": f"/next?item={index}"})
        for index in range(5)
    ]
    driver = _FakeDriver(
        {
            START_URL: _PageState(title="T" * 2_000, body="B" * 100, elements=elements),
        }
    )
    backend = _backend(
        driver,
        limits=BrowserGuidanceLimits(max_visible_chars=10, max_elements=2),
    )

    result = backend.inspect(
        WebPageInspectRequest(url=START_URL, goal="find service"),
        _context(),
    )

    assert result.outcome == "success"
    assert result.content == "B" * 10
    assert len(result.title) == 1_000
    assert [element.ref for element in result.elements] == ["e1", "e2"]
    assert driver.browsers[0].launch_kwargs["headless"] is True
    resolver_rules = next(
        str(argument)
        for argument in driver.browsers[0].launch_kwargs["args"]
        if str(argument).startswith("--host-resolver-rules=")
    )
    assert f"MAP public.example {PUBLIC_IP}" in resolver_rules
    assert "MAP * ~NOTFOUND" in resolver_rules
    assert driver.browsers[0].contexts[0].kwargs == {
        "accept_downloads": False,
        "service_workers": "block",
    }
    assert set(driver.selectors) <= {
        "body",
        "a[href]",
        "button[aria-expanded]",
        "xpath=ancestor::form",
    }
    _assert_all_resources_closed(driver)


def test_inspect_extracts_only_same_origin_links_and_non_submit_expand_buttons() -> None:
    driver = _FakeDriver(
        {
            START_URL: _PageState(
                title="Safe page",
                body="body",
                elements=[
                    _Element("a", "Safe link", {"href": "/next"}),
                    _Element("a", "External", {"href": "https://other.example/"}),
                    _Element("a", "Download", {"href": "/file", "download": "file.txt"}),
                    _Element("a", "Popup", {"href": "/next", "target": "_blank"}),
                    _Element("a", "Script", {"href": "javascript:alert(1)"}),
                    _Element("button", "Expand", {"aria-expanded": "false"}),
                    _Element(
                        "button",
                        "Submit",
                        {"aria-expanded": "false", "type": "submit"},
                    ),
                    _Element(
                        "button",
                        "In form",
                        {"aria-expanded": "false", "type": "button"},
                        inside_form=True,
                    ),
                ],
            )
        }
    )

    result = _backend(driver).inspect(
        WebPageInspectRequest(url=START_URL, goal="safe actions"),
        _context(),
    )

    assert [(item.role, item.name, item.href) for item in result.elements] == [
        ("link", "Safe link", NEXT_URL),
        ("button", "Expand", None),
    ]


def test_explore_replays_prior_actions_in_a_new_context_and_rejects_unknown_ref() -> None:
    driver = _FakeDriver(
        {
            START_URL: _PageState(
                title="Start",
                body="start",
                elements=[_Element("a", "Next", {"href": "/next"})],
            ),
            NEXT_URL: _PageState(title="Next", body="next"),
        }
    )
    backend = _backend(driver)
    initial = backend.inspect(
        WebPageInspectRequest(url=START_URL, goal="next"),
        _context(),
    )

    clicked = backend.explore(
        WebPageExploreRequest(
            browser_session_id=initial.browser_session_id,
            action="click",
            element_ref="e1",
        ),
        _context(),
    )
    replayed = backend.explore(
        WebPageExploreRequest(
            browser_session_id=initial.browser_session_id,
            action="inspect",
        ),
        _context(),
    )
    rejected = backend.explore(
        WebPageExploreRequest(
            browser_session_id=initial.browser_session_id,
            action="click",
            element_ref="e99",
        ),
        _context(),
    )

    assert str(clicked.url) == NEXT_URL
    assert str(replayed.url) == NEXT_URL
    assert driver.browsers[2].contexts[0].pages[0].goto_urls == [START_URL, NEXT_URL]
    assert rejected.outcome == "blocked"
    assert rejected.errors[0].code == "invalid_element_ref"
    assert len(driver.browsers) == 4
    _assert_all_resources_closed(driver)


@pytest.mark.parametrize(
    ("effect", "expected_code"),
    [
        ("download", "download_blocked"),
        ("popup", "new_window_blocked"),
        ("submit", "unsafe_request_method"),
        ("cross_origin", "cross_origin_navigation"),
        ("same_origin_navigation", "unexpected_navigation"),
    ],
)
def test_expand_click_rejects_download_submit_new_window_and_document_navigation(
    effect: str,
    expected_code: str,
) -> None:
    driver = _FakeDriver(
        {
            START_URL: _PageState(
                title="Start",
                body="start",
                elements=[
                    _Element(
                        "button",
                        "Expand",
                        {"aria-expanded": "false", "type": "button"},
                        click_effect=effect,
                    )
                ],
            ),
            NEXT_URL: _PageState(title="Next", body="next"),
        }
    )
    backend = _backend(driver)
    initial = backend.inspect(
        WebPageInspectRequest(url=START_URL, goal="expand"),
        _context(),
    )

    result = backend.explore(
        WebPageExploreRequest(
            browser_session_id=initial.browser_session_id,
            action="click",
            element_ref="e1",
        ),
        _context(),
    )

    assert result.outcome == "blocked"
    assert result.errors[0].code == expected_code
    _assert_all_resources_closed(driver)


def test_final_url_is_revalidated_and_timeout_has_stable_error_code() -> None:
    unsafe_final = "https://public.example/unsafe-final"
    policy = _RecordingPolicy(rejected={unsafe_final})
    final_driver = _FakeDriver(
        {
            START_URL: _PageState(
                title="Start",
                body="start",
                final_url=unsafe_final,
            )
        }
    )

    final_result = _backend(final_driver, policy=policy).inspect(
        WebPageInspectRequest(url=START_URL, goal="inspect"),
        _context(),
    )

    timeout_driver = _FakeDriver(
        {START_URL: _PageState(title="", body="", timeout=True)}
    )
    timeout_result = _backend(timeout_driver).inspect(
        WebPageInspectRequest(url=START_URL, goal="inspect"),
        _context(),
    )

    assert final_result.outcome == "blocked"
    assert final_result.errors[0].code == "unsafe_final_url"
    assert policy.calls == [START_URL, START_URL, unsafe_final]
    assert timeout_result.outcome == "failed"
    assert timeout_result.errors[0].code == "page_timeout"
    assert timeout_result.errors[0].recoverable is True
    _assert_all_resources_closed(final_driver)
    _assert_all_resources_closed(timeout_driver)


def test_explore_enforces_action_limit_before_launching_browser() -> None:
    driver = _FakeDriver({START_URL: _PageState(title="Start", body="start")})
    backend = _backend(driver, limits=BrowserGuidanceLimits(max_actions_per_session=1))
    initial = backend.inspect(
        WebPageInspectRequest(url=START_URL, goal="inspect"),
        _context(),
    )

    first = backend.explore(
        WebPageExploreRequest(
            browser_session_id=initial.browser_session_id,
            action="inspect",
        ),
        _context(),
    )
    second = backend.explore(
        WebPageExploreRequest(
            browser_session_id=initial.browser_session_id,
            action="inspect",
        ),
        _context(),
    )

    assert first.outcome == "success"
    assert second.outcome == "blocked"
    assert second.errors[0].code == "action_limit_exceeded"
    assert len(driver.browsers) == 2


class _SmokeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/next":
            body = b"<html><title>Next</title><body>local next page</body></html>"
        else:
            body = (
                b"<html><title>Start</title><body>local start "
                b"<a href='/next'>Next page</a>"
                b"<button type='button' aria-expanded='false'>Details</button>"
                b"</body></html>"
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        pass


def _chromium_processes() -> set[int]:
    processes: set[int] = set()
    for cmdline_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            cmdline = cmdline_path.read_bytes().replace(b"\0", b" ").lower()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b"chromium" in cmdline and b"playwright" in cmdline:
            processes.add(int(cmdline_path.parent.name))
    return processes


@pytest.mark.playwright_smoke
def test_local_chromium_smoke_inspects_and_explores_without_process_leak() -> None:
    assert playwright_browser_ready()
    before = _chromium_processes()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SmokeHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_address[1]
    start_url = f"http://127.0.0.1:{port}/"

    def local_policy(url: str) -> ValidatedWebTarget:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != port:
            raise WebUrlValidationError("unsafe_url")
        return _target(url, address="127.0.0.1")

    try:
        backend = PlaywrightWebsiteGuidanceBackend(url_policy=local_policy)
        inspected = backend.inspect(
            WebPageInspectRequest(url=start_url, goal="find next page"),
            _context(),
        )
        explored = backend.explore(
            WebPageExploreRequest(
                browser_session_id=inspected.browser_session_id,
                action="click",
                element_ref="e1",
            ),
            _context(),
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert inspected.outcome == "success"
    assert inspected.title == "Start"
    assert [element.name for element in inspected.elements] == ["Next page", "Details"]
    assert explored.outcome == "success"
    assert explored.title == "Next"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (_chromium_processes() - before):
        time.sleep(0.05)
    assert _chromium_processes() - before == set()

from datetime import datetime, timezone

import pytest

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.website_guidance.backend import (
    MockWebsiteGuidanceBackend,
)
from assistant_agent.tools.plugins.builtin.website_guidance.models import (
    WebPageElement,
    WebPageExploreRequest,
    WebPageGuidanceError,
    WebPageGuidanceResult,
    WebPageInspectRequest,
)
from assistant_agent.tools.plugins.builtin.website_guidance.tools import (
    WebPageExploreTool,
    WebPageInspectTool,
)


def test_inspect_tool_returns_bounded_untrusted_observation() -> None:
    tool = WebPageInspectTool(backend=MockWebsiteGuidanceBackend())

    result = tool.run(
        {"url": "https://example.com/service", "goal": "查找办理入口"},
        ToolContext(run_id="run-1", session_id="session-1"),
    )

    assert result.success is True
    assert result.data["outcome"] == "success"
    assert result.data["requested_url"] == "https://example.com/service"
    assert result.data["final_url"] == "https://example.com/service"
    assert datetime.fromisoformat(result.data["checked_at"]).tzinfo is not None
    assert result.model_observation["content_trust"] == "untrusted_external_content"
    assert result.model_observation["requested_url"] == "https://example.com/service"
    assert result.model_observation["final_url"] == "https://example.com/service"
    assert result.model_observation["is_complete"] is True
    assert result.model_observation["elements"][0]["ref"] == "e1"


def test_explore_is_dangerous_and_unverified_mock_url_cannot_succeed() -> None:
    tool = WebPageExploreTool(backend=MockWebsiteGuidanceBackend())

    result = WebPageInspectTool(backend=MockWebsiteGuidanceBackend()).run(
        {"url": "https://unverified.example/service", "goal": "查找办理入口"},
        ToolContext(run_id="run-1", session_id="session-1"),
    )

    assert tool.category == "dangerous"
    assert result.success is False
    assert result.data["outcome"] == "blocked"
    assert result.data["final_url"] is None
    assert result.data["errors"][0]["code"] == "mock_url_unverified"


def test_mock_explore_requires_a_session_issued_to_the_same_owner() -> None:
    backend = MockWebsiteGuidanceBackend()
    explore = WebPageExploreTool(backend=backend)
    owner = ToolContext(run_id="run-1", session_id="session-1")

    unissued = explore.run(
        {
            "browser_session_id": "mock-browser-session-1",
            "action": "inspect",
        },
        owner,
    )
    inspected = WebPageInspectTool(backend=backend).run(
        {"url": "https://example.com/service", "goal": "查找办理入口"},
        owner,
    )
    cross_owner = explore.run(
        {
            "browser_session_id": inspected.data["browser_session_id"],
            "action": "inspect",
        },
        ToolContext(run_id="run-2", session_id="session-2"),
    )

    assert unissued.success is False
    assert unissued.data["errors"][0]["code"] == "browser_session_unavailable"
    assert inspected.success is True
    assert cross_owner.success is False
    assert cross_owner.data["errors"][0]["code"] == "browser_session_unavailable"


def test_mock_terminal_cleanup_revokes_only_that_runs_sessions() -> None:
    backend = MockWebsiteGuidanceBackend()
    inspect = WebPageInspectTool(backend=backend)
    explore = WebPageExploreTool(backend=backend)
    first_owner = ToolContext(run_id="run-1", session_id="session-1")
    second_owner = ToolContext(run_id="run-2", session_id="session-2")
    first = inspect.run(
        {"url": "https://example.com/service", "goal": "查找办理入口"},
        first_owner,
    )
    second = inspect.run(
        {"url": "https://example.com/service", "goal": "查找办理入口"},
        second_owner,
    )

    inspect.on_run_terminal("run-1", "completed")

    revoked = explore.run(
        {
            "browser_session_id": first.data["browser_session_id"],
            "action": "inspect",
        },
        first_owner,
    )
    retained = explore.run(
        {
            "browser_session_id": second.data["browser_session_id"],
            "action": "inspect",
        },
        second_owner,
    )
    assert revoked.success is False
    assert retained.success is True


def test_success_result_requires_verified_final_url_and_aware_check_time() -> None:
    with pytest.raises(ValueError):
        WebPageGuidanceResult(
            outcome="success",
            url="https://example.com/service",
            requested_url="https://example.com/service",
            final_url=None,
            checked_at=datetime.now(timezone.utc),
            browser_session_id="opaque-session-1",
        )

    with pytest.raises(ValueError):
        WebPageGuidanceResult(
            outcome="success",
            url="https://example.com/service",
            requested_url="https://example.com/service",
            final_url="https://example.com/service",
            checked_at=datetime(2026, 8, 3),
            browser_session_id="opaque-session-1",
        )


def test_click_requires_element_ref_and_selectors_are_not_exposed() -> None:
    with pytest.raises(ValueError):
        WebPageExploreRequest(browser_session_id="opaque-session-1", action="click")

    schema = WebPageExploreRequest.model_json_schema()["properties"]
    assert "selector" not in schema
    assert "javascript" not in schema


class _ResultBackend:
    def __init__(self, result: WebPageGuidanceResult) -> None:
        self.result = result
        self.cleaned_runs: list[str] = []

    def inspect(
        self,
        request: WebPageInspectRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        return self.result

    def explore(
        self,
        request: WebPageExploreRequest,
        context: ToolContext,
    ) -> WebPageGuidanceResult:
        return self.result

    def cleanup_run(self, run_id: str) -> int:
        self.cleaned_runs.append(run_id)
        return 1


def _result(
    outcome: str = "success",
    *,
    content: str = "",
    elements: list[WebPageElement] | None = None,
    warnings: list[str] | None = None,
    errors: list[WebPageGuidanceError] | None = None,
) -> WebPageGuidanceResult:
    result_errors = list(errors or [])
    if outcome in {"blocked", "failed"} and not result_errors:
        result_errors.append(
            WebPageGuidanceError(
                code="mock_error",
                message="mock error",
            )
        )
    return WebPageGuidanceResult(
        outcome=outcome,
        url="https://example.com/service",
        requested_url="https://example.com/service",
        final_url=(
            "https://example.com/service"
            if outcome in {"success", "partial"}
            else None
        ),
        checked_at=datetime.now(timezone.utc),
        browser_session_id="opaque-session-1",
        content=content,
        elements=elements or [],
        warnings=warnings or [],
        errors=result_errors,
    )


def test_requests_and_tools_reject_unknown_navigation_inputs() -> None:
    with pytest.raises(ValueError):
        WebPageInspectRequest.model_validate(
            {
                "url": "https://example.com/service",
                "goal": "查找办理入口",
                "selector": "#apply",
            }
        )
    with pytest.raises(ValueError):
        WebPageExploreRequest.model_validate(
            {
                "browser_session_id": "opaque-session-1",
                "action": "inspect",
                "javascript": "alert('unsafe')",
            }
        )

    inspect_result = WebPageInspectTool(backend=MockWebsiteGuidanceBackend()).run(
        {
            "url": "https://example.com/service",
            "goal": "查找办理入口",
            "selector": "#apply",
        }
    )
    explore_result = WebPageExploreTool(backend=MockWebsiteGuidanceBackend()).run(
        {
            "browser_session_id": "opaque-session-1",
            "action": "inspect",
            "javascript": "alert('unsafe')",
        }
    )

    assert inspect_result.success is False
    assert explore_result.success is False


def test_inspect_tool_terminal_hook_delegates_run_scoped_cleanup() -> None:
    backend = _ResultBackend(_result())
    tool = WebPageInspectTool(backend=backend)

    tool.on_run_terminal("run-terminal", "cancelled")

    assert backend.cleaned_runs == ["run-terminal"]


def test_observation_bounds_and_outcome_success_mapping() -> None:
    elements = [
        WebPageElement(
            ref=f"e{index}",
            role="link",
            name=f"办理入口 {index}",
            safe_action="click",
        )
        for index in range(1, 42)
    ]
    errors = [
        WebPageGuidanceError(
            code=f"mock_error_{index}",
            message=f"mock error {index}",
            recoverable=True,
        )
        for index in range(6)
    ]
    partial_result = WebPageInspectTool(
        backend=_ResultBackend(
            _result(
                "partial",
                content="x" * 12_001,
                elements=elements,
                warnings=[f"warning {index}" for index in range(11)],
                errors=errors,
            )
        )
    ).run({"url": "https://example.com/service", "goal": "查找办理入口"})

    assert partial_result.success is True
    assert len(partial_result.model_observation["content"]) == 12_000
    assert len(partial_result.model_observation["elements"]) == 40
    assert len(partial_result.model_observation["warnings"]) == 10
    assert len(partial_result.model_observation["errors"]) == 5

    for outcome in ("blocked", "failed"):
        result = WebPageInspectTool(backend=_ResultBackend(_result(outcome))).run(
            {"url": "https://example.com/service", "goal": "查找办理入口"}
        )
        assert result.success is False

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
    assert result.model_observation["content_trust"] == "untrusted_external_content"
    assert result.model_observation["elements"][0]["ref"] == "e1"


def test_click_requires_element_ref_and_selectors_are_not_exposed() -> None:
    with pytest.raises(ValueError):
        WebPageExploreRequest(browser_session_id="opaque-session-1", action="click")

    schema = WebPageExploreRequest.model_json_schema()["properties"]
    assert "selector" not in schema
    assert "javascript" not in schema


class _ResultBackend:
    def __init__(self, result: WebPageGuidanceResult) -> None:
        self.result = result

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


def _result(
    outcome: str = "success",
    *,
    content: str = "",
    elements: list[WebPageElement] | None = None,
    warnings: list[str] | None = None,
    errors: list[WebPageGuidanceError] | None = None,
) -> WebPageGuidanceResult:
    return WebPageGuidanceResult(
        outcome=outcome,
        url="https://example.com/service",
        browser_session_id="opaque-session-1",
        content=content,
        elements=elements or [],
        warnings=warnings or [],
        errors=errors or [],
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

import pytest

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.website_guidance.backend import (
    MockWebsiteGuidanceBackend,
)
from assistant_agent.tools.plugins.builtin.website_guidance.models import (
    WebPageExploreRequest,
)
from assistant_agent.tools.plugins.builtin.website_guidance.tools import (
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

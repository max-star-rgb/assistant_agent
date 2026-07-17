from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.services.web_fetch_adapter import MockWebFetchAdapter
from assistant_agent.tools.registry import create_default_registry
from assistant_agent.tools.web_fetch_tool import WebFetchTool


def _validate_web_fetch(tool_input: dict[str, object]):
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="Open https://example.com/article and read the page content",
    )
    return ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call", tool_name="web_fetch", tool_input=tool_input
        ),
        registry=create_default_registry(),
        request=request,
        state=AgentState.from_request(request),
    )


def test_default_registry_includes_web_fetch_as_external_read() -> None:
    registry = create_default_registry()

    assert "web_fetch" in registry.list()
    spec = next(spec for spec in registry.list_specs() if spec.name == "web_fetch")
    assert spec.side_effect.level == "external_read"
    assert spec.side_effect.requires_confirmation is False
    assert "url" in spec.required_inputs


def test_action_validator_rejects_empty_web_fetch_url() -> None:
    validation = _validate_web_fetch({"url": "   "})

    assert validation.accepted is False
    assert validation.code == "invalid_tool_input"
    assert validation.message == "web_fetch requires url."


def test_action_validator_rejects_non_http_web_fetch_url() -> None:
    validation = _validate_web_fetch({"url": "file:///tmp/page.html"})

    assert validation.accepted is False
    assert validation.code == "invalid_tool_input"
    assert validation.message == "web_fetch requires http or https url."


def test_action_validator_accepts_valid_web_fetch_url() -> None:
    validation = _validate_web_fetch(
        {"url": "https://example.com/article", "max_chars": 1200}
    )

    assert validation.accepted is True
    assert validation.code == "accepted"


def test_web_fetch_tool_returns_structured_result_and_contract() -> None:
    result = WebFetchTool(adapter=MockWebFetchAdapter()).run(
        {"url": "https://example.com/article", "max_chars": 80}
    )

    assert result.success is True
    assert result.tool_name == "web_fetch"
    assert result.output_ref == "mock://web_fetch/example-com-article"
    assert result.data["provider"] == "mock"
    assert result.data["url"] == "https://example.com/article"
    assert result.data["title"] == "Mock page for https://example.com/article"
    assert result.data["content"]
    assert result.data["total_chars"] >= len(result.data["content"])
    assert result.contract is not None
    assert result.contract.capability == "web_fetch"


def test_web_fetch_observation_summarizes_page_and_preserves_content() -> None:
    result = WebFetchTool(adapter=MockWebFetchAdapter()).run(
        {"url": "https://example.com/article", "max_chars": 120}
    )

    observation = observation_from_tool_result(result)

    assert observation.status == "succeeded"
    assert "Fetched web page" in observation.summary
    assert "https://example.com/article" in observation.summary
    assert observation.structured_output["url"] == "https://example.com/article"
    assert observation.structured_output["content"]
    assert observation.next_step_hint is not None
    assert "page content" in observation.next_step_hint

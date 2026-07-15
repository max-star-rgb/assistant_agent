from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.services.web_search_adapter import MockWebSearchAdapter
from assistant_agent.tools.registry import create_default_registry
from assistant_agent.tools.web_search_tool import WebSearchTool


def _validate_web_search(tool_input: dict[str, object]):
    request = UserRequest(
        user_id="u1", session_id="s1", text="联网搜索今天 AI 最新消息"
    )
    return ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call", tool_name="web_search", tool_input=tool_input
        ),
        registry=create_default_registry(),
        request=request,
        state=AgentState.from_request(request),
    )


def test_default_registry_includes_web_search_as_external_read() -> None:
    registry = create_default_registry()

    assert "web_search" in registry.list()
    spec = next(spec for spec in registry.list_specs() if spec.name == "web_search")
    assert spec.side_effect.level == "external_read"
    assert spec.side_effect.requires_confirmation is False
    assert "query" in spec.required_inputs


def test_action_validator_rejects_empty_web_search_query() -> None:
    validation = _validate_web_search({"query": "   "})

    assert validation.accepted is False
    assert validation.code == "invalid_tool_input"
    assert validation.message == "web_search requires query."


def test_action_validator_accepts_valid_web_search_query_and_limit() -> None:
    validation = _validate_web_search({"query": "OpenAI latest news", "limit": 3})

    assert validation.accepted is True
    assert validation.code == "accepted"


def test_action_validator_rejects_web_search_limit_outside_schema() -> None:
    validation = _validate_web_search({"query": "OpenAI latest news", "limit": 25})

    assert validation.accepted is False
    assert validation.code == "invalid_tool_input"
    assert "less than or equal to 10" in validation.message


def test_web_search_tool_returns_structured_result_and_contract() -> None:
    result = WebSearchTool(adapter=MockWebSearchAdapter()).run(
        {"query": "OpenAI latest news", "recency_days": 7, "limit": 2}
    )

    assert result.success is True
    assert result.tool_name == "web_search"
    assert result.output_ref == "mock://web_search/openai-latest-news"
    assert result.data["provider"] == "mock"
    assert result.data["query_used"] == "OpenAI latest news"
    assert len(result.data["results"]) == 2
    assert result.data["results"][0]["title"]
    assert result.data["results"][0]["url"].startswith("mock://web-search/")
    assert result.contract is not None
    assert result.contract.capability == "web_search"


def test_web_search_observation_summarizes_first_result_and_preserves_sources() -> None:
    result = WebSearchTool(adapter=MockWebSearchAdapter()).run(
        {"query": "OpenAI latest news", "limit": 2}
    )

    observation = observation_from_tool_result(result)

    assert observation.status == "succeeded"
    assert "Top web result of 2" in observation.summary
    assert "mock://web-search/" in observation.summary
    assert observation.structured_output["results"][0]["title"]
    assert observation.structured_output["results"][0]["published_at"]

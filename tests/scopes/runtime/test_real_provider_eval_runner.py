from pathlib import Path

import pytest

from assistant_agent.agent.state import AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.eval.real_provider import (
    EvalConfigurationError,
    RealProviderEvalCase,
    controlled_tool_provider_config,
    evaluate_real_provider_state,
    load_real_provider_eval_cases,
    validate_real_provider_config,
    write_real_provider_eval_artifacts,
)
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import RunToolSet
from assistant_agent.services.trace_store import TraceEvent


def test_real_provider_eval_requires_explicit_real_chat_provider() -> None:
    with pytest.raises(EvalConfigurationError, match="provider_smoke or pilot"):
        validate_real_provider_config(ProviderConfig.from_env({}))

    with pytest.raises(EvalConfigurationError, match="explicit chat provider"):
        validate_real_provider_config(
            ProviderConfig.from_env(
                {
                    "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
                    "DEEPSEEK_CHAT_API_KEY": "test-key",
                }
            )
        )

    validate_real_provider_config(
        ProviderConfig.from_env(
            {
                "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
                "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
                "DEEPSEEK_CHAT_API_KEY": "test-key",
            }
        )
    )


def test_real_provider_eval_defaults_non_chat_providers_to_controlled_mock() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-key",
            "MULTIMODAL_AGENT_SEARCH_PROVIDER": "http",
            "MULTIMODAL_AGENT_WEB_SEARCH_API_KEY": "search-key",
        }
    )

    controlled = controlled_tool_provider_config(config)

    assert controlled.chat_provider == "deepseek"
    assert controlled.chat_adapter_kind == config.chat_adapter_kind
    assert controlled.search_provider == "mock"
    assert controlled.web_search_api_key is None
    assert controlled.product_search_provider == "mock"
    assert controlled.memory_backend == "memory"
    assert controlled_tool_provider_config(config, allow_real_tools=True) is config


def test_real_provider_eval_scores_tool_flow_and_response_coverage() -> None:
    case = RealProviderEvalCase(
        id="briefing_morning_weather_calendar",
        text="早上好，帮我说下今天出门前要注意什么",
        expected_tools=["weather", "calendar_search"],
        expected_tool_sequence=["weather", "calendar_search"],
        expected_exposed_tools=["weather", "calendar_search"],
        must_not_call=["web_search"],
        response_must_include_any=[["天气", "气温"], ["日程", "会议"], ["雨伞", "雨具"]],
    )
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text=case.text))
    state.run_tool_set = RunToolSet(
        registered_tool_names=["weather", "calendar_search", "web_search"],
        qualified_tool_names=["weather", "calendar_search", "web_search"],
        exposed_tool_names=["weather", "calendar_search", "web_search"],
        executable_tool_names=["weather", "calendar_search", "web_search"],
    )
    state.add_tool_call("weather", {"location": "上海"})
    state.add_tool_call("calendar_search", {"query": "今天"})
    state.set_response(AgentResponse(message="今天上海天气晴，上午有会议，出门不用带雨伞。"))

    detail = evaluate_real_provider_state(case, state, trace_events=[])

    assert detail.passed is True
    assert detail.actual_tools == ["weather", "calendar_search"]
    assert detail.checks["expected_tools_match"] is True
    assert detail.checks["response_keyword_groups_match"] is True


def test_real_provider_eval_reports_missing_tools_and_exposure() -> None:
    case = RealProviderEvalCase(
        id="briefing_missing_calendar",
        text="出门前提醒我一下",
        expected_tools=["weather", "calendar_search"],
        expected_exposed_tools=["weather", "calendar_search"],
        must_not_call=["web_search"],
    )
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text=case.text))
    state.run_tool_set = RunToolSet(
        registered_tool_names=["weather", "calendar_search", "web_search"],
        qualified_tool_names=["weather", "calendar_search", "web_search"],
        exposed_tool_names=["web_search"],
        executable_tool_names=["web_search"],
        excluded_reasons={
            "weather": ["entry_profile_not_exposed"],
            "calendar_search": ["entry_profile_not_exposed"],
        },
    )
    state.add_tool_call("web_search", {"query": "today travel tips"})
    state.set_response(AgentResponse(message="我查了网页，建议注意天气。"))

    detail = evaluate_real_provider_state(case, state, trace_events=[])

    assert detail.passed is False
    assert detail.missing_expected_tools == ["weather", "calendar_search"]
    assert detail.unexpected_tools == ["web_search"]
    assert detail.missing_exposed_tools == ["weather", "calendar_search"]
    assert detail.excluded_reasons["calendar_search"] == ["entry_profile_not_exposed"]


def test_real_provider_eval_writes_machine_readable_artifacts(tmp_path: Path) -> None:
    case = RealProviderEvalCase(id="briefing_smoke", text="早上好")
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text=case.text))
    state.set_response(AgentResponse(message="早上好。"))
    detail = evaluate_real_provider_state(case, state, trace_events=[])

    artifact = write_real_provider_eval_artifacts(
        output_root=tmp_path,
        suite_name="personal_assistant_briefing",
        provider="deepseek",
        model="deepseek-test",
        cases=[case],
        details=[detail],
        trace_events=[
            TraceEvent(
                trace_id=state.trace_id,
                run_id=state.run_id,
                node_name="native_runtime",
                event_type="observability",
                canonical_event="run.started",
            )
        ],
    )

    assert artifact.run_dir.exists()
    assert artifact.summary_path.exists()
    assert artifact.results_path.exists()
    assert artifact.trace_path.exists()
    assert artifact.cases_path.exists()


def test_real_provider_eval_loads_seed_personal_assistant_cases() -> None:
    cases = load_real_provider_eval_cases(Path("evals/real_provider/personal_assistant_briefing.json"))

    assert len(cases) >= 20
    assert any(case.id == "personal_departure_morning_001" for case in cases)
    assert all(case.expected_status == "completed" for case in cases)

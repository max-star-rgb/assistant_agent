"""Offline contract for the controlled meeting-logistics Mission."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.decision_models import AssistantDecision
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.ids import PYTHON_INTERPRETER_TOOL_NAME
from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingSearchRequest,
)
from evals.agent.missions.meeting_logistics_tentative_calendar_commit.environment import (
    _MeetingLodgingAdapter,
)
from evals.agent.loader import load_case_source, load_entrypoint, load_task
from evals.agent.travel_support import GEO_TOOL, POI_TOOL, TRANSIT_TOOL


TASK_ID = "meeting_logistics_tentative_calendar_commit"


class _NoCallChat:
    provider = "no-call"
    model = "no-call"

    def chat(self, *_: Any, **__: Any) -> None:
        raise AssertionError("Environment validation must not run the Agent.")


def _mock_config() -> ProviderConfig:
    return ProviderConfig(provider_mode="mock")


def test_meeting_logistics_mission_declares_one_capability() -> None:
    task = load_task(TASK_ID)
    source = load_case_source(TASK_ID)

    assert source.level == "mission"
    assert task.capability == "constraint_aware_meeting_logistics_commit"
    assert task.environment.endswith(":MeetingLogisticsEnvironment")
    assert task.grader.endswith(":grade")
    assert task.request.metadata["tool_visibility"]["enabled_tools"] == [
        "calendar_create"
    ]


def test_meeting_logistics_environment_controls_dependencies_and_state() -> None:
    task = load_task(TASK_ID)
    environment = load_entrypoint(task.environment)(
        config=_mock_config(),
        chat_adapter=_NoCallChat(),
    )

    validation = environment.validate()
    expectations = {
        item.tool_name: item
        for item in environment.tool_outcome_expectations()
    }

    assert validation.passed is True
    assert {
        POI_TOOL,
        GEO_TOOL,
        TRANSIT_TOOL,
        "lodging_search",
        "calendar_create",
    } <= set(expectations)
    assert all(
        expectations[name].required
        for name in (
            POI_TOOL,
            GEO_TOOL,
            TRANSIT_TOOL,
            "lodging_search",
            "calendar_create",
        )
    )
    assert {"web_search", "web_fetch"}.isdisjoint(expectations)


def test_meeting_lodging_wrong_query_does_not_leak_fixture_oracle() -> None:
    result = _MeetingLodgingAdapter().search(
        LodgingSearchRequest(
            destination="上海",
            check_in=date(2026, 9, 17),
            check_out=date(2026, 9, 19),
            adults=8,
            rooms=3,
            nearby_poi="上海青浦万达茂",
            max_nightly_price=600,
            sort="distance_asc",
        )
    )

    assert result.success is True
    assert result.offers == []
    assert result.provider_notice is None


def test_meeting_environment_python_succeeds_without_global_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "MULTIMODAL_AGENT_PYTHON_INTERPRETER_ENABLED",
        raising=False,
    )
    environment = load_entrypoint(load_task(TASK_ID).environment)(
        config=_mock_config(),
        chat_adapter=_NoCallChat(),
    )

    result = environment._build_registry().run(
        PYTHON_INTERPRETER_TOOL_NAME,
        {"code": "result = 6 * 7"},
    )

    assert result.success is True
    assert result.data["result_json"] == 42
    assert result.data["errors"] == []


def test_meeting_environment_python_keeps_tool_owned_safety() -> None:
    environment = load_entrypoint(load_task(TASK_ID).environment)(
        config=_mock_config(),
        chat_adapter=_NoCallChat(),
    )
    registry = environment._build_registry()
    request = UserRequest(
        user_id="eval-meeting-logistics-user",
        session_id="eval-meeting-logistics-session",
        text="计算会议费用",
    )

    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=PYTHON_INTERPRETER_TOOL_NAME,
            tool_input={"code": 'open("secret.txt")'},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert validation.accepted is False
    assert validation.code == "unsafe_tool_input"

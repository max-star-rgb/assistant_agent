from __future__ import annotations

from assistant_agent.api.models import agent_run_response_from_state
from assistant_agent.runtime import assistant_loop_nodes
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult, ProviderSearchSource
from assistant_agent.runtime.output_models import AssistantTextOutput
from assistant_agent.runtime.planning_models import IntentResult
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState


def _request() -> UserRequest:
    return UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="query-sentinel",
    )


def _chat_result() -> ChatResult:
    return ChatResult(
        response_text="answer-sentinel [1]",
        provider="qwen",
        model="model-sentinel",
        search_sources=[ProviderSearchSource(
            index=1,
            title="source-sentinel",
            url="https://example.com/1",
        )],
    )


def test_native_provider_citations_reach_http_response() -> None:
    decision = assistant_loop_nodes._native_final_decision(_chat_result())
    state = AgentState.from_request(
        _request(),
        run_id="run-sentinel",
        trace_id="trace-sentinel",
    )
    state.set_response(AgentResponse(
        message=decision.text,
        annotations=decision.annotations,
    ))

    response = agent_run_response_from_state(state)

    assert response.response_text == "answer-sentinel [1]"
    assert [item.model_dump(mode="json") for item in response.annotations] == [{
        "type": "url_citation",
        "start_index": 16,
        "end_index": 19,
        "source_id": "source_1",
        "title": "source-sentinel",
        "url": "https://example.com/1",
    }]


class _DirectChatAdapter:
    def chat(self, request: ChatRequest) -> ChatResult:
        return _chat_result()


def test_direct_chat_persists_provider_citations_on_agent_response() -> None:
    request = _request()
    state = AgentState.from_request(request)
    state.set_intent(IntentResult(
        intent="direct_chat",
        confidence=1.0,
        rationale="test-sentinel",
    ))

    assistant_loop_nodes._set_direct_chat_response(
        {
            "request": request,
            "state": state,
            "chat_adapter": _DirectChatAdapter(),
        },
        AssistantTextOutput(text="fallback-sentinel"),
        1,
        [],
    )

    assert state.response is not None
    assert [item.source_id for item in state.response.annotations] == ["source_1"]


def test_tool_final_answer_persists_decision_citations_on_agent_response() -> None:
    request = _request()
    state = AgentState.from_request(request)
    decision = assistant_loop_nodes._native_final_decision(_chat_result())

    assistant_loop_nodes._set_assistant_final_answer_response(
        {"request": request, "state": state},
        decision,
        2,
        [{"tool_name": "probe_tool", "success": True}],
    )

    assert state.response is not None
    assert [item.source_id for item in state.response.annotations] == ["source_1"]

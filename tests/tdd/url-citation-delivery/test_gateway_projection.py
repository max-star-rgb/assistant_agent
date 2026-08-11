from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from assistant_agent.gateway.runtime_adapter import GatewayRuntimeAdapter
from assistant_agent.gateway.runtime_types import RealtimeAgentRequest, RealtimeAgentResult
from assistant_agent.gateway.session import _run_end_payload
from assistant_agent.runtime.citations import UrlCitationAnnotation
from assistant_agent.runtime.requests import AgentResponse
from assistant_agent.runtime.state import AgentState


def _annotation() -> UrlCitationAnnotation:
    return UrlCitationAnnotation(
        start_index=16,
        end_index=19,
        source_id="source_1",
        title="source-sentinel",
        url="https://example.com/1",
    )


def test_gateway_runtime_result_preserves_agent_response_annotations() -> None:
    annotation = _annotation()

    def run_request(request: Any, **kwargs: Any) -> Any:
        state = AgentState.from_request(request)
        state.set_response(AgentResponse(
            message="answer-sentinel [1]",
            annotations=[annotation],
        ))
        return SimpleNamespace(
            runtime=SimpleNamespace(trace_store=None),
            state=state,
            events=[],
        )

    result = asyncio.run(GatewayRuntimeAdapter(
        run_request=run_request,
        load_env=False,
        enable_conversation_history=False,
    ).run_turn(RealtimeAgentRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="query-sentinel",
    )))

    assert [item.model_dump(mode="json") for item in result.annotations] == [
        annotation.model_dump(mode="json")
    ]


def test_run_end_delivers_annotations_only_for_completed_terminal() -> None:
    annotation = _annotation()
    completed = _run_end_payload(
        result=RealtimeAgentResult(
            status="completed",
            response_text="answer-sentinel [1]",
            annotations=[annotation],
        ),
        expects_reply=False,
        run_id="run-sentinel",
    )
    completed_without_annotations = _run_end_payload(
        result=RealtimeAgentResult(status="completed", response_text="plain-sentinel"),
        expects_reply=False,
        run_id="run-sentinel",
    )
    cancelled = _run_end_payload(
        result=RealtimeAgentResult(status="cancelled", annotations=[annotation]),
        expects_reply=False,
        run_id="run-sentinel",
    )

    assert completed["annotations"] == [annotation.model_dump(mode="json")]
    assert "annotations" not in completed_without_annotations
    assert "annotations" not in cancelled

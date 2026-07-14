from __future__ import annotations

import asyncio

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime_profile import get_runtime_profile
from assistant_agent.schemas.realtime_turn_arbitration import (
    RealtimeTurnArbitrationRequest,
    normalize_arbitration_decision,
)
from assistant_agent.services.chat_adapter import ChatProviderError, ChatRequest, ChatResult
from assistant_agent.services.realtime_turn_arbiter import (
    ChatAdapterRealtimeTurnArbiter,
    ConservativeRealtimeTurnArbiter,
    create_realtime_turn_arbiter,
)


def _request() -> RealtimeTurnArbitrationRequest:
    return RealtimeTurnArbitrationRequest(
        decision_id="decision-1",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-2",
        run_id="run-2",
        expected_run_id="run-1",
        utterance="不是北京，改成上海",
        language="zh-CN",
        task_state={
            "objective": "查询北京周末天气",
            "constraints": [],
            "pending_tool": None,
            "committed_side_effect_count": 0,
        },
    )


def test_normalize_arbitration_decision_rebinds_identity_and_rejects_low_confidence() -> None:
    decision = normalize_arbitration_decision(
        {
            "decision_id": "model-controlled",
            "disposition": "REVISE_ACTIVE",
            "revision_type": "replace_constraint",
            "confidence": 0.50,
            "reason_code": "Corrects Active Constraint!",
            "expected_run_id": "attacker-run",
        },
        request=_request(),
        min_confidence=0.80,
        source="semantic_llm",
    )

    assert decision.disposition == "UNCERTAIN"
    assert decision.expected_run_id == "run-1"
    assert decision.decision_id == "decision-1"
    assert decision.reason_code == "corrects_active_constraint"
    assert decision.fallback_reason == "low_confidence"


def test_normalize_arbitration_decision_enforces_revision_matrix() -> None:
    decision = normalize_arbitration_decision(
        {
            "disposition": "REPLACE_ACTIVE",
            "revision_type": "add_constraint",
            "confidence": 0.98,
            "reason_code": "changes_goal",
        },
        request=_request(),
        min_confidence=0.80,
        source="semantic_llm",
    )

    assert decision.disposition == "REPLACE_ACTIVE"
    assert decision.revision_type == "change_goal"
    assert decision.fallback_reason is None


class _ScriptedChatAdapter:
    provider = "scripted"

    def __init__(self, result: ChatResult) -> None:
        self.result = result
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return self.result


def test_chat_adapter_arbiter_requests_bounded_json_without_tools() -> None:
    adapter = _ScriptedChatAdapter(
        ChatResult(
            response_text=(
                '{"disposition":"REVISE_ACTIVE","revision_type":"replace_constraint",'
                '"confidence":0.93,"reason_code":"corrects_active_constraint"}'
            ),
            provider="scripted",
            model="arbiter-test",
        )
    )
    arbiter = ChatAdapterRealtimeTurnArbiter(adapter, min_confidence=0.80)

    decision = asyncio.run(arbiter.arbitrate(_request()))

    assert decision.disposition == "REVISE_ACTIVE"
    assert decision.revision_type == "replace_constraint"
    assert decision.source == "semantic_llm"
    assert len(adapter.requests) == 1
    chat_request = adapter.requests[0]
    assert chat_request.tools == []
    assert chat_request.tool_choice is None
    assert chat_request.response_format == {"type": "json_object"}
    assert chat_request.temperature == 0.0
    assert chat_request.max_tokens == 256
    assert "查询北京周末天气" in chat_request.user_query
    assert len(chat_request.user_query) < 6000


def test_chat_adapter_arbiter_accepts_fenced_json() -> None:
    adapter = _ScriptedChatAdapter(
        ChatResult(
            response_text=(
                "```json\n"
                '{"disposition":"FOLLOWUP","confidence":0.96,"reason_code":"new_task"}'
                "\n```"
            ),
            provider="scripted",
        )
    )

    decision = asyncio.run(
        ChatAdapterRealtimeTurnArbiter(adapter, min_confidence=0.80).arbitrate(_request())
    )

    assert decision.disposition == "FOLLOWUP"
    assert decision.fallback_reason is None


def test_chat_adapter_arbiter_falls_back_on_provider_error() -> None:
    adapter = _ScriptedChatAdapter(
        ChatResult(
            provider="scripted",
            errors=[
                ChatProviderError(
                    code="provider_timeout",
                    message="provider did not answer",
                    recoverable=True,
                )
            ],
        )
    )

    decision = asyncio.run(
        ChatAdapterRealtimeTurnArbiter(adapter, min_confidence=0.80).arbitrate(_request())
    )

    assert decision.disposition == "UNCERTAIN"
    assert decision.fallback_reason == "provider_error"


def test_chat_adapter_arbiter_falls_back_on_invalid_json() -> None:
    adapter = _ScriptedChatAdapter(
        ChatResult(response_text="not-json", provider="scripted")
    )

    decision = asyncio.run(
        ChatAdapterRealtimeTurnArbiter(adapter, min_confidence=0.80).arbitrate(_request())
    )

    assert decision.disposition == "UNCERTAIN"
    assert decision.fallback_reason == "invalid_model_output"


def test_arbiter_factory_is_conservative_outside_real_provider_profiles() -> None:
    adapter = _ScriptedChatAdapter(
        ChatResult(
            response_text=(
                '{"disposition":"CANCEL_ONLY","confidence":0.99,'
                '"reason_code":"cancel_request"}'
            ),
            provider="scripted",
        )
    )

    arbiter = create_realtime_turn_arbiter(
        ProviderConfig(runtime_profile=get_runtime_profile("local_demo")),
        adapter,
        min_confidence=0.80,
    )
    decision = asyncio.run(arbiter.arbitrate(_request()))

    assert isinstance(arbiter, ConservativeRealtimeTurnArbiter)
    assert decision.disposition == "UNCERTAIN"
    assert decision.fallback_reason == "llm_arbiter_disabled"
    assert adapter.requests == []


def test_arbiter_factory_enables_non_mock_adapter_in_pilot() -> None:
    adapter = _ScriptedChatAdapter(
        ChatResult(
            response_text=(
                '{"disposition":"ACK_NOOP","confidence":0.97,'
                '"reason_code":"backchannel"}'
            ),
            provider="scripted",
        )
    )

    arbiter = create_realtime_turn_arbiter(
        ProviderConfig(runtime_profile=get_runtime_profile("pilot")),
        adapter,
        min_confidence=0.80,
    )

    assert isinstance(arbiter, ChatAdapterRealtimeTurnArbiter)

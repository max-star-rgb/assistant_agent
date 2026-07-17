from assistant_agent.agent.intent import IntentDetector
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest


def test_rule_decision_url_read_uses_web_fetch() -> None:
    decision = IntentDetector().detect_decision(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="Read https://example.com/article page content",
        )
    )

    assert decision.primary_intent == "web_fetch"
    assert decision.capabilities == ["web_fetch"]
    assert [step.tool_name for step in decision.plan_steps] == ["web_fetch"]


def test_offline_runtime_executes_web_fetch_for_url_read_request() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="Read https://example.com/article page content",
        )
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "web_fetch"
    assert [call.tool_name for call in state.tool_calls] == ["web_fetch"]
    assert state.tool_calls[0].input["url"] == "https://example.com/article"
    assert state.tool_results[0].success is True
    assert state.tool_results[0].data["provider"] == "mock"

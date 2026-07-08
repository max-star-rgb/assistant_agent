from datetime import datetime, timezone

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.memory_observability import load_memory_with_trace
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.tools.registry import create_default_registry


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class CountingMemoryStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.search_count = 0

    def search(self, query):
        self.search_count += 1
        return super().search(query)


def _seed_store(store: InMemoryStore) -> None:
    store.save(
        MemoryItem(
            memory_id="m-black-bag",
            user_id="u1",
            session_id="s-old",
            memory_type="product",
            summary="用户之前关注过黑色通勤包。",
            created_at=NOW,
        )
    )


def test_auto_memory_load_skips_store_without_explicit_read_intent() -> None:
    store = CountingMemoryStore()
    _seed_store(store)
    manager = MemoryManager(store)
    request = UserRequest(user_id="u1", session_id="s1", text="帮我写一句黑色包商品短文案")
    state = AgentState.from_request(request)

    context = manager.load_into_state(state, request)

    assert store.search_count == 0
    assert context.items == []
    assert state.memory_context == []
    assert state.request.metadata["memory_context_skipped"] is True
    assert state.request.metadata["memory_context_policy_reason"] == "memory_read_intent_not_detected"
    assert state.request.metadata["memory_context_injected_ids"] == []


def test_auto_memory_load_allows_previous_context_intent() -> None:
    store = CountingMemoryStore()
    _seed_store(store)
    manager = MemoryManager(store)
    request = UserRequest(user_id="u1", session_id="s1", text="继续上次黑色包任务")
    state = AgentState.from_request(request)

    context = manager.load_into_state(state, request)

    assert store.search_count == 1
    assert [item.memory_id for item in context.items] == ["m-black-bag"]
    assert state.request.metadata["memory_context_skipped"] is False
    assert state.request.metadata["memory_context_policy_reason"] == "explicit_memory_reference"


def test_load_memory_trace_records_skipped_read_policy_without_memory_text() -> None:
    store = CountingMemoryStore()
    _seed_store(store)
    manager = MemoryManager(store)
    trace_store = InMemoryTraceStore()
    request = UserRequest(user_id="u1", session_id="s1", text="帮我写一句黑色包商品短文案")
    state = AgentState.from_request(request)

    load_memory_with_trace(
        manager=manager,
        trace_store=trace_store,
        trace_id=state.trace_id,
        node_name="load_memory",
        state=state,
        request=request,
    )

    finished = [
        event for event in trace_store.list_by_trace(state.trace_id)
        if event.canonical_event == "memory.load.finished"
    ][0]
    assert store.search_count == 0
    assert finished.status == "skipped"
    assert finished.attributes["read_policy"]["allowed"] is False
    assert finished.attributes["read_policy"]["reason"] == "memory_read_intent_not_detected"
    assert finished.output_summary["memory"]["read_policy"]["allowed"] is False
    assert "用户之前关注过黑色通勤包" not in str(finished.model_dump(mode="json"))


def test_action_validator_rejects_memory_retrieval_without_read_intent() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="帮我写一句浅色背景文案")
    state = AgentState.from_request(request)

    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="memory_retrieval",
            tool_input={"query": "浅色背景"},
        ),
        registry=create_default_registry(),
        request=request,
        state=state,
    )

    assert validation.accepted is False
    assert validation.code == "memory_read_intent_required"
    assert validation.metadata["memory_read_policy"]["allowed"] is False


def test_action_validator_accepts_memory_retrieval_for_saved_preference_intent() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="按我保存的偏好写一句浅色背景文案")
    state = AgentState.from_request(request)

    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="memory_retrieval",
            tool_input={"query": "保存的偏好"},
        ),
        registry=create_default_registry(),
        request=request,
        state=state,
    )

    assert validation.accepted is True
    assert validation.metadata["memory_read_policy"]["allowed"] is True
    assert validation.metadata["memory_read_policy"]["reason"] == "explicit_memory_reference"


def test_action_validator_rejects_legacy_memory_retrieve_without_read_intent() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="给我一个早餐建议")
    state = AgentState.from_request(request)

    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="memory",
            tool_input={"action": "retrieve", "user_id": "u1", "query": "早餐"},
        ),
        registry=create_default_registry(),
        request=request,
        state=state,
    )

    assert validation.accepted is False
    assert validation.code == "memory_read_intent_required"

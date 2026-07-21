from typing import Any

from assistant_agent.agent.state import AgentState
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.schemas.requests import UserRequest


class SlowAuditStore:
    def __init__(self) -> None:
        self.audit_save_calls = 0

    def save_audit_event(self, _event: Any) -> None:
        self.audit_save_calls += 1
        raise AssertionError("skipped memory reads must not enter the persistent audit backend")


def test_skipped_automatic_memory_read_does_not_touch_persistent_store() -> None:
    store = SlowAuditStore()
    manager = MemoryManager(store)  # type: ignore[arg-type]
    request = UserRequest(user_id="user-1", session_id="session-1", text="你好，介绍你自己")
    state = AgentState.from_request(request)

    context = manager.load_into_state(state, request)

    assert context.read_policy_allowed is False
    assert request.metadata["memory_context_skipped"] is True
    assert store.audit_save_calls == 0

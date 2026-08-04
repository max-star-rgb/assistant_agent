from types import SimpleNamespace

from assistant_agent.gateway import runtime_pool as runtime_pool_module
from assistant_agent.runtime.assistant_runtime_app import AssistantRuntimeApp


class _CoordinatorStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def clear_session(self, user_id, session_id):
        self.calls.append(("session", user_id, session_id))
        return True

    def clear_user(self, user_id):
        self.calls.append(("user", user_id))
        return 2


class _SessionStore:
    def delete(self, _user_id, _session_id):
        return True

    def delete_by_user(self, _user_id):
        return 1


class _Memory:
    def clear_session(self, **_kwargs):
        return None

    def clear_user(self, **_kwargs):
        return None


def _runtime(store, visual_store):
    return SimpleNamespace(
        embedding_coordinator_store=store,
        visual_semantic_store_pool=visual_store,
        session_store=_SessionStore(),
        long_term_memory_service=_Memory(),
        run_history=None,
        trace_store=SimpleNamespace(delete_by_user=lambda _user_id: 0),
        config=SimpleNamespace(),
    )


def test_runtime_app_delete_boundaries_clear_temporal_session_and_user(monkeypatch) -> None:
    store = _CoordinatorStore()
    visual_store = _CoordinatorStore()
    app = AssistantRuntimeApp(lambda: _runtime(store, visual_store))
    monkeypatch.setattr(
        "assistant_agent.runtime.assistant_runtime_app.clear_conversation_history",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "assistant_agent.runtime.assistant_runtime_app.clear_user_conversation_history",
        lambda *_args, **_kwargs: 0,
    )

    assert app.delete_session("user-1", "session-1") is True
    app.delete_user_runtime_data("user-1")

    assert store.calls == [
        ("session", "user-1", "session-1"),
        ("user", "user-1"),
    ]
    assert visual_store.calls == [
        ("session", "user-1", "session-1"),
        ("user", "user-1"),
    ]


def test_shared_gateway_runtime_factory_passes_one_embedding_store(monkeypatch) -> None:
    store = object()
    visual_store = object()
    primary = SimpleNamespace(
        config=object(),
        agent_id="agent",
        long_term_memory_service=object(),
        session_store=object(),
        trace_store=object(),
        video_context_store=object(),
        realtime_video_memory_store=object(),
        durable_task_service=object(),
        embedding_coordinator_store=store,
        visual_semantic_store_pool=visual_store,
    )
    captured = {}
    monkeypatch.setattr(
        runtime_pool_module,
        "AgentGraphRuntime",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(**kwargs),
    )
    factory = runtime_pool_module.shared_gateway_runtime_factory(lambda: primary)

    assert factory() is primary
    factory()

    assert captured["embedding_coordinator_store"] is store
    assert captured["visual_semantic_store_pool"] is visual_store

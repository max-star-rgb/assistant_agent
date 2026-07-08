from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.sessions import SessionCreate
from assistant_agent.services.assistant_runtime_app import AssistantRuntimeApp
from assistant_agent.services.trace_store import InMemoryTraceStore


def test_assistant_runtime_app_runs_request_and_query_through_shared_service() -> None:
    runtime = AgentGraphRuntime(config=ProviderConfig(), trace_store=InMemoryTraceStore())
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)

    request_artifacts = app.run_request(
        UserRequest(user_id="u1", session_id="s1", text="你好"),
        load_env=False,
    )
    query_artifacts = app.run_query(
        "生成一张日系海报",
        user_id="u1",
        session_id="s1",
        load_env=False,
        metadata={"source": "test"},
    )

    assert request_artifacts.api_response().run_id.startswith("run_")
    assert query_artifacts.api_response().response_text
    assert app.runtime_info()["providers"]["chat"] == "mock"


def test_assistant_runtime_app_wraps_sessions_trace_and_memory_services() -> None:
    memory_store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=memory_store, trace_store=InMemoryTraceStore())
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)

    session = app.create_session(SessionCreate(user_id="u1", title="Test"))
    artifacts = app.run_request(
        UserRequest(user_id="u1", session_id=session.session_id, text="记住我喜欢极简风"),
        load_env=False,
    )

    assert app.list_sessions("u1").total == 1
    assert app.get_session("u1", session.session_id) is not None
    assert app.trace_query().run_summary(artifacts.state.run_id) is not None
    assert app.memory_audit_service().audit(user_id="u1").user_id == "u1"
    assert app.memory_snapshot_service().snapshot(user_id="u1").user_id == "u1"
    assert app.delete_session("u1", session.session_id) is True


def test_assistant_runtime_app_deletes_user_runtime_data() -> None:
    runtime = AgentGraphRuntime(memory_store=InMemoryStore(), trace_store=InMemoryTraceStore())
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    app.create_session(SessionCreate(user_id="u1", title="Test"))
    app.run_request(UserRequest(user_id="u1", session_id="s1", text="你好"), load_env=False)

    deleted = app.delete_user_runtime_data("u1")

    assert deleted["trace_events"] > 0
    assert deleted["session_records"] >= 1
    assert app.trace_query().run_summary("missing") is None

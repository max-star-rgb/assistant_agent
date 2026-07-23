"""Stable Mem0 capture and read contracts used by the runtime memory lifecycle."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.framework.adapters import Mem0MemoryEngineAdapter
from assistant_agent.memory.framework.base import FrameworkHttpRequest
from assistant_agent.memory.framework.ledger import FrameworkGovernanceLedger
from assistant_agent.memory.framework.store import FrameworkMemoryStore
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_store import sanitize_trace_value
from assistant_agent.schemas.memory_framework import (
    FrameworkConversationMessage,
    FrameworkRecallRequest,
    FrameworkRetainRequest,
    FrameworkTurnCaptureRequest,
    MemoryEngineIdentity,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.memory.tools import MemorySearchTool


def _identity() -> MemoryEngineIdentity:
    return MemoryEngineIdentity(
        bank_id="bank_" + "1" * 32,
        user_id="usr_" + "2" * 32,
        agent_id="agt_" + "3" * 32,
        run_id="run_" + "4" * 32,
        tenant_tag="tenant_" + "5" * 24,
        user_tag="user_" + "6" * 24,
        project_tag="project_" + "7" * 24,
        session_tag="session_" + "8" * 24,
    )


def test_mem0_sidecar_resolves_repo_dotenv_qwen_settings() -> None:
    module_path = (
        Path(__file__).resolve().parents[3]
        / "docker"
        / "memory-frameworks"
        / "mem0_env.py"
    )
    spec = importlib.util.spec_from_file_location("test_mem0_env", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    resolved = module.resolve_mem0_provider_environment(
        {
            "OPENAI_MODEL": "",
            "MEMORY_BAKEOFF_CHAT_MODEL": "qwen-memory-model",
            "QWEN_CHAT_MODEL": "qwen-main-model",
            "QWEN_API_KEY": "test-qwen-key",
            "QWEN_CHAT_BASE_URL": "https://qwen.test/v1",
        }
    )

    assert resolved == {
        "chat_model": "qwen-memory-model",
        "chat_api_key": "test-qwen-key",
        "chat_base_url": "https://qwen.test/v1",
        "embedding_model": "text-embedding-v4",
        "embedding_api_key": "test-qwen-key",
        "embedding_base_url": "https://qwen.test/v1",
    }


def test_mem0_explicit_retain_is_recallable_as_core_memory() -> None:
    requests: list[FrameworkHttpRequest] = []

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        return {"results": [{"id": "explicit-core-1"}]}

    adapter = Mem0MemoryEngineAdapter(
        base_url="http://mem0.test",
        transport=transport,
    )
    result = adapter.retain(
        FrameworkRetainRequest(
            identity=_identity(),
            project_memory_id="project-memory-1",
            text="stable-memory-sentinel",
            memory_type="preference",
            scope="project",
            source="explicit_user_request",
            created_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
            idempotency_key="retain-1",
        )
    )

    assert result.accepted is True
    assert requests[0].body is not None
    assert requests[0].body["infer"] is False
    assert requests[0].body["metadata"]["record_kind"] == "core"


def test_mem0_update_replaces_source_text_through_native_put() -> None:
    requests: list[FrameworkHttpRequest] = []

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        return {"id": "engine-memory-1", "memory": "edited-memory-sentinel"}

    adapter = Mem0MemoryEngineAdapter(
        base_url="http://mem0.test",
        transport=transport,
    )

    updated = adapter.update(
        identity=_identity(),
        engine_id="engine-memory-1",
        text="edited-memory-sentinel",
    )

    assert updated is True
    assert requests == [
        FrameworkHttpRequest(
            method="PUT",
            path="/memories/engine-memory-1",
            body={"memory": "edited-memory-sentinel"},
            headers=None,
        )
    ]


def test_governed_mem0_edit_keeps_project_memory_id(tmp_path) -> None:
    requests: list[FrameworkHttpRequest] = []
    engine_text = {"value": "original-memory-sentinel"}

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        if request.method == "POST":
            return {"results": [{"id": "engine-memory-1"}]}
        if request.method == "PUT":
            assert request.body is not None
            engine_text["value"] = str(request.body["memory"])
            return {"id": "engine-memory-1", "memory": engine_text["value"]}
        return {
            "id": "engine-memory-1",
            "memory": engine_text["value"],
            "metadata": {
                "project_memory_id": "stable-project-memory-1",
                "memory_type": "preference",
                "scope": "project",
                "source": "explicit_user_request",
                "record_kind": "core",
            },
        }

    store = FrameworkMemoryStore(
        adapter=Mem0MemoryEngineAdapter(
            base_url="http://mem0.test",
            transport=transport,
        ),
        ledger=FrameworkGovernanceLedger(tmp_path / "framework-ledger.sqlite3"),
        identity_namespace="tests",
    )
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(
        user_id="owner-user",
        project_id="owner-project",
        session_id="editing-session",
    )
    saved = manager.save_explicit_for_identity(
        identity,
        memory_id="stable-project-memory-1",
        text="original-memory-sentinel",
        content={"summary": "original-memory-sentinel"},
        scope="project",
    )

    updated = manager.update_explicit_for_identity(
        identity,
        memory_id="stable-project-memory-1",
        text="edited-memory-sentinel",
    )

    assert updated is not None
    assert updated.memory_id == saved.memory_id == "stable-project-memory-1"
    assert updated.summary == "edited-memory-sentinel"
    put_request = next(request for request in requests if request.method == "PUT")
    assert put_request.path == "/memories/engine-memory-1"
    assert put_request.body == {"memory": "edited-memory-sentinel"}
    mappings = store.ledger.list_mappings(
        user_id="owner-user",
        project_memory_id="stable-project-memory-1",
    )
    assert [mapping.engine_id for mapping in mappings] == ["engine-memory-1"]


def test_mem0_capture_stores_daily_record_and_infers_core_memory() -> None:
    requests: list[FrameworkHttpRequest] = []

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        return {"results": [{"id": "daily-1" if len(requests) == 1 else "core-1"}]}

    adapter = Mem0MemoryEngineAdapter(base_url="http://mem0.test", transport=transport)
    result = adapter.capture_turn(
        FrameworkTurnCaptureRequest(
            identity=_identity(),
            messages=[
                FrameworkConversationMessage(role="user", content="我想喝牛奶"),
                FrameworkConversationMessage(role="assistant", content="需要我帮你找商品吗？"),
            ],
            daily_text="用户：我想喝牛奶\n助手：需要我帮你找商品吗？",
            daily_memory_id="daily-turn-1",
            occurred_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
            metadata={"source_turn": "opaque-turn"},
            idempotency_key="capture-1",
        )
    )

    assert result.accepted is True
    assert result.daily_engine_ids == ["daily-1"]
    assert result.core_engine_ids == ["core-1"]
    assert result.errors == []
    assert len(requests) == 2

    daily = requests[0].body
    core = requests[1].body
    assert daily is not None and core is not None
    assert daily["infer"] is False
    assert daily["messages"] == [
        {"role": "user", "content": "用户：我想喝牛奶\n助手：需要我帮你找商品吗？"}
    ]
    assert daily["metadata"]["record_kind"] == "daily"
    assert core["infer"] is True
    assert core["messages"] == [
        {"role": "user", "content": "我想喝牛奶"},
        {"role": "assistant", "content": "需要我帮你找商品吗？"},
    ]
    assert core["metadata"]["record_kind"] == "core"
    assert daily["user_id"] == core["user_id"]
    assert daily["agent_id"] == core["agent_id"]
    assert "run_id" not in daily and "run_id" not in core


def test_mem0_capture_reports_partial_failure_without_dropping_daily_success() -> None:
    requests: list[FrameworkHttpRequest] = []

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        if len(requests) == 2:
            raise RuntimeError("core provider unavailable")
        return {"results": [{"id": "daily-1"}]}

    adapter = Mem0MemoryEngineAdapter(base_url="http://mem0.test", transport=transport)
    result = adapter.capture_turn(
        FrameworkTurnCaptureRequest(
            identity=_identity(),
            messages=[
                FrameworkConversationMessage(role="user", content="你好"),
                FrameworkConversationMessage(role="assistant", content="你好！"),
            ],
            daily_text="用户：你好\n助手：你好！",
            daily_memory_id="daily-turn-2",
            occurred_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
            idempotency_key="capture-2",
        )
    )

    assert result.accepted is True
    assert result.daily_engine_ids == ["daily-1"]
    assert result.core_engine_ids == []
    assert result.errors == [
        {"phase": "core", "code": "memory_framework_request_failed"}
    ]


def test_mem0_unavailable_recall_is_a_failed_memory_tool_result(tmp_path) -> None:
    def unavailable_transport(request: FrameworkHttpRequest):
        raise RuntimeError(f"{request.path} unavailable")

    store = FrameworkMemoryStore(
        adapter=Mem0MemoryEngineAdapter(
            base_url="http://mem0.test",
            transport=unavailable_transport,
        ),
        ledger=FrameworkGovernanceLedger(tmp_path / "framework-ledger.sqlite3"),
        identity_namespace="tests",
    )
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(
        user_id="memory-user",
        session_id="memory-session",
    )

    result = MemorySearchTool().run(
        {"query": "昨天喝了什么"},
        ToolContext(
            user_id=identity.user_id,
            session_id=identity.session_id,
            metadata={
                "memory_manager": manager,
                "request_identity": identity.model_dump(mode="json"),
            },
        ),
    )

    assert result.success is False
    assert result.error == "memory service is temporarily unavailable"
    assert result.contract is not None
    assert result.contract.status == "failed"
    assert [error.code for error in result.contract.errors] == [
        "memory_framework_recall_failed"
    ]
    assert result.model_observation is not None
    assert result.model_observation["status"] == "failed"


def test_mem0_unavailable_runtime_records_recall_and_capture_failures(tmp_path) -> None:
    def unavailable_transport(request: FrameworkHttpRequest):
        raise RuntimeError(f"{request.path} unavailable")

    store = FrameworkMemoryStore(
        adapter=Mem0MemoryEngineAdapter(
            base_url="http://mem0.test",
            transport=unavailable_transport,
        ),
        ledger=FrameworkGovernanceLedger(tmp_path / "framework-ledger.sqlite3"),
        identity_namespace="tests",
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        memory_store=store,
        session_store=InMemorySessionStore(),
    )

    initialized = runtime.initialize_session_memory(
        RequestIdentity.for_user(
            user_id="capture-user",
            session_id="capture-session",
        )
    )
    state = runtime.run_state(
        UserRequest(user_id="capture-user", session_id="capture-session", text="你好")
    )

    assert state.status == "completed"
    assert state.request.metadata["memory_capture"]["status"] == "queued"
    assert runtime.drain_memory_captures(timeout=10.0) is True
    recall = next(
        event
        for event in runtime.trace_store.list_by_run(initialized.run_id)
        if event.canonical_event == "memory.load.finished"
    )
    events = runtime.trace_store.list_by_run(state.run_id)
    capture = next(
        event for event in events if event.canonical_event == "memory.capture.finished"
    )
    assert recall.status == "degraded"
    assert recall.error is not None
    assert recall.error["code"] == "memory_framework_recall_failed"
    assert capture.status == "failed"
    assert capture.error is not None
    assert capture.error["code"] == "memory_framework_capture_failed"
    assert capture.error["detail"]["errors"] == [
        {"phase": "daily", "code": "memory_framework_request_failed"},
        {"phase": "core", "code": "memory_framework_request_failed"},
    ]
    runtime.close()
    assert sanitize_trace_value("") == ""


def test_mem0_recall_filters_core_and_daily_records_separately() -> None:
    requests: list[FrameworkHttpRequest] = []

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        return {"results": []}

    adapter = Mem0MemoryEngineAdapter(base_url="http://mem0.test", transport=transport)
    adapter.recall(
        FrameworkRecallRequest(
            identity=_identity(),
            query="牛奶",
            scope="project",
            record_kinds=["daily"],
        )
    )

    body = requests[0].body
    assert body is not None
    assert body["filters"] == {
        "user_id": _identity().user_id,
        "agent_id": _identity().agent_id,
        "record_kind": "daily",
    }


def test_completed_runtime_turn_triggers_mem0_capture(tmp_path) -> None:
    requests: list[FrameworkHttpRequest] = []

    def transport(request: FrameworkHttpRequest):
        requests.append(request)
        if request.path == "/search":
            return {"results": []}
        if request.method == "GET" and request.path.startswith("/memories/"):
            return {
                "id": request.path.rsplit("/", 1)[-1],
                "memory": "时间：2026-07-22T12:00:00+08:00\n用户：你好\n助手结果：你好！",
                "metadata": {"record_kind": "daily", "source": "runtime_turn_capture"},
            }
        capture_index = sum(1 for item in requests if item.path == "/memories")
        return {"results": [{"id": f"captured-{capture_index}"}]}

    store = FrameworkMemoryStore(
        adapter=Mem0MemoryEngineAdapter(
            base_url="http://mem0.test",
            transport=transport,
        ),
        ledger=FrameworkGovernanceLedger(tmp_path / "framework-ledger.sqlite3"),
        identity_namespace="tests",
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        memory_store=store,
        session_store=InMemorySessionStore(),
    )

    runtime.initialize_session_memory(
        RequestIdentity.for_user(
            user_id="capture-user",
            session_id="capture-session",
        )
    )
    state = runtime.run_state(
        UserRequest(user_id="capture-user", session_id="capture-session", text="你好")
    )

    assert state.status == "completed"
    assert state.request.metadata["memory_context_policy_reason"] == (
        "framework_core_memory_auto_load"
    )
    assert state.request.metadata["memory_capture"]["status"] == "queued"
    assert runtime.drain_memory_captures(timeout=10.0) is True
    assert requests[0].path == "/search"
    assert requests[0].body is not None
    assert requests[0].body["filters"]["record_kind"] == "core"
    assert [request.path for request in requests[-2:]] == ["/memories", "/memories"]

    daily_mapping = next(
        mapping
        for mapping in store.ledger.list_mappings(user_id="capture-user")
        if mapping.project_memory_id.startswith("daily:")
    )
    daily_item = runtime.memory_manager.get_for_identity(
        RequestIdentity.from_user_request(state.request),
        daily_mapping.project_memory_id,
    )
    assert daily_item is not None
    assert daily_item.content["record_kind"] == "daily"
    runtime.close()

# Unified Runtime Entry Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move product entry layers behind a single `AssistantRuntimeApp` boundary before wiring observers.

**Architecture:** `AssistantRuntimeApp` owns direct access to `AgentGraphRuntime` for product paths and delegates assistant execution to `run_assistant_request`. HTTP, WebSocket, Gateway backend factory, and the local offline CLI depend on the app service instead of constructing or passing runtime directly.

**Tech Stack:** Python, FastAPI route modules, existing `assistant_run_service`, pytest.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest.
- Keep default paths mock/local/offline; do not call real providers.
- Use `apply_patch` for manual edits.
- Do not add dependencies.
- Do not change HTTP, WebSocket, Gateway, or CLI response schemas.
- Do not change Gateway frame names or lifecycle semantics.
- Do not move assistant decisions, tool execution, memory policy, provider policy, or routing logic into entry layers.
- Do not wire hook observers in this phase.
- Do not touch unrelated untracked `tmp/` files.

---

### Task 1: Add `AssistantRuntimeApp`

**Files:**
- Create: `src/assistant_agent/services/assistant_runtime_app.py`
- Test: `tests/test_assistant_runtime_app.py`

**Interfaces:**
- Consumes: `run_assistant_request(request, runtime=..., **kwargs)`, `UserRequest`, `ProviderConfig`, `AgentGraphRuntime`, `TraceQueryService`, `MemoryAuditService`, `MemorySnapshotService`, session schemas.
- Produces: `AssistantRuntimeApp(runtime_factory)`, `runtime`, `config`, `run_request`, `run_query`, `runtime_info`, `trace_query`, `memory_audit_service`, `memory_snapshot_service`, `create_session`, `list_sessions`, `get_session`, `delete_session`, and `delete_user_runtime_data`.

- [ ] **Step 1: Write failing tests**

Add `tests/test_assistant_runtime_app.py`:

```python
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
    assert app.memory_audit_service().audit("u1").user_id == "u1"
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
```

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_runtime_app.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'assistant_agent.services.assistant_runtime_app'`.

- [ ] **Step 3: Implement the app service**

Create `src/assistant_agent/services/assistant_runtime_app.py` with:

```python
"""Application runtime boundary for product entry layers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.sessions import SessionCreate, SessionList, SessionRecord
from assistant_agent.schemas.memory_snapshot import MemoryStorageSnapshot
from assistant_agent.services.assistant_run_service import (
    AssistantRunArtifacts,
    clear_conversation_history,
    clear_user_conversation_history,
    get_default_conversation_store,
    run_assistant_request,
    runtime_info,
)
from assistant_agent.services.memory_audit import MemoryAuditService
from assistant_agent.services.memory_snapshot import MemorySnapshotService
from assistant_agent.services.trace_query import TraceQueryService


class AssistantRuntimeApp:
    """Product entry boundary over the internal AgentGraphRuntime."""

    def __init__(self, runtime_factory: Callable[[], AgentGraphRuntime]) -> None:
        self._runtime_factory = runtime_factory

    @property
    def runtime(self) -> AgentGraphRuntime:
        return self._runtime_factory()

    @property
    def config(self) -> ProviderConfig:
        return self.runtime.config

    def run_request(self, request: UserRequest, **kwargs: Any) -> AssistantRunArtifacts:
        return run_assistant_request(request, runtime=self.runtime, **kwargs)

    def run_query(
        self,
        query: str,
        *,
        image_refs: list[str] | None = None,
        video_refs: list[str] | None = None,
        user_id: str = "demo_user",
        session_id: str = "demo_session",
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AssistantRunArtifacts:
        return self.run_request(
            UserRequest(
                user_id=user_id,
                session_id=session_id,
                text=query,
                image_ids=list(image_refs or []),
                video_ids=list(video_refs or []),
                metadata=metadata or {"source": "assistant_runtime_app"},
            ),
            **kwargs,
        )

    def runtime_info(self) -> dict[str, Any]:
        return runtime_info(self.config)

    def trace_query(self) -> TraceQueryService:
        return TraceQueryService(self.runtime.trace_store)

    def memory_audit_service(self) -> MemoryAuditService:
        return MemoryAuditService(self.runtime.memory_manager)

    def memory_snapshot_service(self) -> MemorySnapshotService:
        runtime = self.runtime
        conversation_store = get_default_conversation_store(runtime.config)
        return MemorySnapshotService(
            memory_manager=runtime.memory_manager,
            session_store=runtime.session_store,
            conversation_store=conversation_store,
            storage=MemoryStorageSnapshot(
                memory_store=type(runtime.memory_store).__name__,
                session_store=type(runtime.session_store).__name__,
                conversation_store=type(conversation_store).__name__,
                checkpointer=type(runtime.checkpointer).__name__ if runtime.checkpointer is not None else "none",
            ),
        )

    def create_session(self, session: SessionCreate) -> SessionRecord:
        return self.runtime.session_store.create(session)

    def list_sessions(self, user_id: str) -> SessionList:
        sessions = self.runtime.session_store.list_by_user(user_id)
        return SessionList(user_id=user_id, total=len(sessions), sessions=sessions)

    def get_session(self, user_id: str, session_id: str) -> SessionRecord | None:
        return self.runtime.session_store.get(user_id, session_id)

    def delete_session(self, user_id: str, session_id: str) -> bool:
        runtime = self.runtime
        deleted = runtime.session_store.delete(user_id, session_id)
        if deleted:
            clear_conversation_history(user_id, session_id, config=runtime.config)
        return deleted

    def delete_user_runtime_data(self, user_id: str) -> dict[str, int]:
        runtime = self.runtime
        memory_items = runtime.memory_manager.list_by_user(user_id)
        runtime.memory_manager.clear_user(user_id)
        run_history_deleted = runtime.run_history.delete_by_user(user_id) if runtime.run_history is not None else 0
        tool_history_deleted = runtime.tool_history.delete_by_user(user_id) if runtime.tool_history is not None else 0
        trace_deleted = runtime.trace_store.delete_by_user(user_id)
        conversation_sessions_deleted = clear_user_conversation_history(user_id, config=runtime.config)
        session_records_deleted = runtime.session_store.delete_by_user(user_id)
        return {
            "memory_items": len(memory_items),
            "run_history_records": run_history_deleted,
            "tool_history_records": tool_history_deleted,
            "trace_events": trace_deleted,
            "conversation_sessions": conversation_sessions_deleted,
            "session_records": session_records_deleted,
        }
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_runtime_app.py -q
```

Expected: PASS.

### Task 2: Migrate Product Entry Modules

**Files:**
- Modify: `src/assistant_agent/api/routes_agent.py`
- Modify: `src/assistant_agent/api/websocket.py`
- Modify: `src/assistant_agent/api/gateway_runtime.py`
- Modify: `scripts/run_assistant_cli.py`
- Test: `tests/test_architecture_boundaries.py`, `tests/test_assistant_cli.py`

**Interfaces:**
- Consumes: `AssistantRuntimeApp`.
- Produces: `routes_agent.get_assistant_runtime_app()` and product entry modules that no longer import or construct `AgentGraphRuntime` directly.

- [ ] **Step 1: Add boundary tests**

Update `tests/test_architecture_boundaries.py` with:

```python
def test_product_entry_layers_do_not_import_agent_graph_runtime_directly() -> None:
    for path in (
        "src/assistant_agent/api/routes_agent.py",
        "src/assistant_agent/api/websocket.py",
        "src/assistant_agent/api/gateway_runtime.py",
        "scripts/run_assistant_cli.py",
    ):
        source = _source(path)
        assert "from assistant_agent.agent.runtime import AgentGraphRuntime" not in source
        assert "AgentGraphRuntime(" not in source


def test_product_entry_layers_depend_on_runtime_app_boundary() -> None:
    for path in (
        "src/assistant_agent/api/routes_agent.py",
        "src/assistant_agent/api/websocket.py",
        "src/assistant_agent/api/gateway_runtime.py",
        "scripts/run_assistant_cli.py",
    ):
        assert "AssistantRuntimeApp" in _source(path) or "get_assistant_runtime_app" in _source(path)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_architecture_boundaries.py::test_product_entry_layers_do_not_import_agent_graph_runtime_directly tests/test_architecture_boundaries.py::test_product_entry_layers_depend_on_runtime_app_boundary -q
```

Expected: FAIL because the modules still import or instantiate `AgentGraphRuntime`.

- [ ] **Step 3: Migrate entry modules**

Make these changes:

- In `routes_agent.py`, import `AssistantRuntimeApp`; remove the direct `AgentGraphRuntime` import; keep `_RUNTIME = None`; implement:

```python
def get_agent_runtime():
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = create_runtime()
    return _RUNTIME


def get_assistant_runtime_app() -> AssistantRuntimeApp:
    return AssistantRuntimeApp(runtime_factory=get_agent_runtime)
```

Then route handlers should call `app = get_assistant_runtime_app()` and use
`app.run_request`, `app.runtime_info`, `app.create_session`, `app.list_sessions`,
`app.get_session`, `app.delete_session`, `app.trace_query`,
`app.memory_audit_service`, `app.memory_snapshot_service`, and
`app.delete_user_runtime_data`.

- In `api/websocket.py`, remove the direct runtime import and helper; use
`routes_agent.get_assistant_runtime_app().run_request(request, event_sink=event_sink)`.

- In `api/gateway_runtime.py`, change the backend callback to:

```python
from assistant_agent.api.routes_agent import get_assistant_runtime_app

return get_assistant_runtime_app().run_request(request, **kwargs)
```

- In `scripts/run_assistant_cli.py`, remove `AgentGraphRuntime` import and call
`AssistantRuntimeApp(runtime_factory=lambda: create_runtime(config=ProviderConfig(), load_env=False)).run_query(...)`.

- [ ] **Step 4: Verify entry tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_architecture_boundaries.py tests/test_assistant_cli.py -q
```

Expected: PASS.

### Task 3: Verify API, WebSocket, Gateway, And Shared Service

**Files:**
- Test only.

**Interfaces:**
- Consumes: migrated `AssistantRuntimeApp` entry boundary.
- Produces: confidence that public product surfaces still behave the same.

- [ ] **Step 1: Run focused product-entry regression**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_runtime_app.py tests/test_shared_assistant_run_service.py tests/test_api_agent_graph_runtime.py tests/test_websocket_graph_runtime.py tests/test_gateway_api.py tests/test_gateway_session.py tests/test_realtime_agent_backend.py -q
```

Expected: PASS.

- [ ] **Step 2: Fix only compatibility issues caused by the migration**

If tests fail because they monkeypatch `routes_agent.get_agent_runtime`, preserve that compatibility by keeping `get_assistant_runtime_app()` backed by the monkeypatchable `get_agent_runtime` global. Do not revert the product-entry migration.

### Task 4: Document And Commit

**Files:**
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Consumes: `AssistantRuntimeApp` boundary.
- Produces: docs that say observer wiring waits until the unified runtime app boundary is stable.

- [ ] **Step 1: Update docs**

In `docs/gateway-architecture.md`, update the non-realtime path to:

```text
CLI / HTTP / Web UI
        |
        v
AssistantRuntimeApp
        |
        v
run_assistant_request
        |
        v
AgentGraphRuntime / assistant loop
```

In `docs/observability-harness.md`, add that hook observers should attach after
entry layers go through `AssistantRuntimeApp`, not directly inside Web/CLI
adapters.

- [ ] **Step 2: Run final verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_runtime_app.py tests/test_architecture_boundaries.py tests/test_assistant_cli.py tests/test_api_agent_graph_runtime.py tests/test_websocket_graph_runtime.py tests/test_gateway_api.py tests/test_gateway_session.py tests/test_realtime_agent_backend.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: PASS.

- [ ] **Step 3: Commit implementation**

```bash
git add src/assistant_agent/services/assistant_runtime_app.py tests/test_assistant_runtime_app.py tests/test_architecture_boundaries.py src/assistant_agent/api/routes_agent.py src/assistant_agent/api/websocket.py src/assistant_agent/api/gateway_runtime.py scripts/run_assistant_cli.py docs/gateway-architecture.md docs/observability-harness.md
git commit -m "feat: unify runtime entry boundary"
```

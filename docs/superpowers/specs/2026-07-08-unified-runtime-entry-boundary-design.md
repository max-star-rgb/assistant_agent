# Unified Runtime Entry Boundary Design

## Goal

Tighten the product entry architecture before wiring more observers. Web, HTTP,
WebSocket, CLI, and Gateway-backed realtime paths should not each decide how to
construct or call `AgentGraphRuntime`. They should depend on one application
runtime boundary, and that boundary should own the direct runtime dependency.

## Problem

The project has the right pieces, but the ownership is loose:

- `assistant_run_service` is already the shared non-Gateway run service.
- `/agent/run` uses `run_assistant_request`, but API routes still expose and
  pass around `AgentGraphRuntime` directly.
- `/ws/agent/{session_id}` reaches through the HTTP route module to reuse the
  runtime singleton.
- Gateway realtime uses `GatewayAgentAdapter`, but the FastAPI-owned Gateway
  runtime calls back into `routes_agent.get_agent_runtime()`.
- `scripts/run_client.py` is a thin server client, but
  `scripts/run_assistant_cli.py` still instantiates `AgentGraphRuntime`
  directly for local offline runs.

This makes the product boundary unclear. It also makes observer integration
risky because there is no single place to attach run-level harness composition.

## Recommended Approach

Create `AssistantRuntimeApp`, a small application service that owns the direct
runtime dependency for product entry layers.

Entry layers will call the app service:

```text
Web UI / HTTP / WebSocket / local CLI
  -> AssistantRuntimeApp
  -> run_assistant_request
  -> AgentGraphRuntime
```

Gateway realtime keeps its lifecycle boundary, but its backend factory also uses
the same app service:

```text
/ws/gateway or /ws/realtime/media
  -> GatewaySessionService
  -> GatewayAgentAdapter
  -> AssistantRuntimeApp
  -> run_assistant_request
  -> AgentGraphRuntime
```

`AgentGraphRuntime` remains the internal executor. This phase does not change
assistant loop behavior, tool calling, memory policy, provider selection, or
Gateway protocol semantics.

## API Shape

Add `src/assistant_agent/services/assistant_runtime_app.py`:

```python
class AssistantRuntimeApp:
    def __init__(self, runtime_factory: Callable[[], AgentGraphRuntime]) -> None: ...

    @property
    def runtime(self) -> AgentGraphRuntime: ...

    @property
    def config(self) -> ProviderConfig: ...

    def run_request(self, request: UserRequest, **kwargs: Any) -> AssistantRunArtifacts: ...

    def run_query(self, query: str, **kwargs: Any) -> AssistantRunArtifacts: ...

    def runtime_info(self) -> dict[str, Any]: ...

    def trace_query(self) -> TraceQueryService: ...

    def memory_audit_service(self) -> MemoryAuditService: ...

    def memory_snapshot_service(self) -> MemorySnapshotService: ...

    def create_session(self, session: SessionCreate) -> SessionRecord: ...

    def list_sessions(self, user_id: str) -> SessionList: ...

    def get_session(self, user_id: str, session_id: str) -> SessionRecord | None: ...

    def delete_session(self, user_id: str, session_id: str) -> bool: ...

    def delete_user_runtime_data(self, user_id: str) -> dict[str, int]: ...
```

The app may expose the underlying runtime through a `runtime` property for
legacy service construction and tests, but product entry modules should prefer
the higher-level app methods. The direct `AgentGraphRuntime` dependency should
be isolated to `assistant_run_service`, `assistant_runtime_app`, lower-level
runtime/realtime modules, tests, and offline smoke/eval scripts.

## Entry Layer Changes

### FastAPI HTTP

`routes_agent.py` should expose `get_assistant_runtime_app()` and use that app
for:

- `/agent/run`;
- runtime info;
- session create/list/get/delete;
- trace query endpoints;
- beta data deletion;
- memory audit and snapshot service construction;
- control-plane trace query construction.

`get_agent_runtime()` can remain as a compatibility helper for tests and legacy
modules during this phase, but route handlers should not call it directly.

### FastAPI WebSocket

`api/websocket.py` should depend on `get_assistant_runtime_app()` and call
`app.run_request(...)`. It should not import `AgentGraphRuntime` or reach into
the HTTP route runtime singleton.

### Gateway Runtime

`api/gateway_runtime.py` should call
`routes_agent.get_assistant_runtime_app().run_request(...)` from the default
backend factory. Gateway remains responsible for session/run/cancel/hangup
lifecycle; the app service only runs the assistant turn.

### Local CLI

`scripts/run_assistant_cli.py` should stop constructing `AgentGraphRuntime`
directly. The local offline path should call `AssistantRuntimeApp.run_query()`
with `ProviderConfig()` and `load_env=False`. `scripts/run_client.py` remains
the preferred product CLI client for the running FastAPI backend.

## Alternatives Considered

### Send all Web and CLI traffic through Gateway

This would force request/response product calls into realtime session semantics
they do not always need. Gateway should remain the normalized realtime lifecycle
boundary, not the only way to run a single assistant request.

### Keep current `get_agent_runtime()` and only add observers there

This is the fastest path, but it preserves the current confusion. Observers
would become attached to a runtime singleton instead of a clear application
boundary, and WebSocket/Gateway/CLI code would remain inconsistent.

### Large rewrite around dependency injection

FastAPI dependency injection could cleanly inject app services everywhere, but a
large rewrite would touch many endpoints at once. This phase should add the app
boundary and migrate the main product paths without changing external schemas.

## Safety And Constraints

- Do not change HTTP, WebSocket, Gateway, or CLI public response schemas.
- Do not enable real providers by default.
- Do not change Gateway frame names or lifecycle semantics.
- Do not move assistant decisions, tool execution, memory policy, provider
  policy, or routing logic into entry layers.
- Do not wire hook observers yet. The observer hook system should attach only
  after this entry boundary is stable.
- Keep tests compatible with existing monkeypatch patterns where practical.

## Tests

Add or update focused tests to prove:

- `AssistantRuntimeApp.run_request()` and `run_query()` use the shared run
  service and return existing artifact shapes.
- HTTP `/agent/run` uses `AssistantRuntimeApp`, not a direct route-level runtime
  call.
- WebSocket `/ws/agent/{session_id}` uses `AssistantRuntimeApp`.
- Gateway default backend uses `AssistantRuntimeApp`.
- `scripts/run_assistant_cli.py` no longer imports or constructs
  `AgentGraphRuntime`.
- Architecture boundary tests guard product entry modules against direct
  `AgentGraphRuntime` imports.
- Existing API/WebSocket/Gateway/CLI tests still pass.

## Stop Criteria

Stop this phase when product entry modules depend on `AssistantRuntimeApp`,
focused API/WebSocket/Gateway/CLI tests pass, and the fast suite passes. Only
then resume observer wiring at the unified app boundary.

# Phase 0 Architecture Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Phase 0 into executable architecture-stability work: entry convergence contracts, tool-governance contracts, trace invariants, and a repeatable architecture gate.

**Architecture:** This phase hardens existing boundaries instead of introducing a new runtime. HTTP `/agent/run`, local CLI `--text`, `/ws/gateway`, and `/ws/realtime/media` must remain Gateway-first; `AgentGraphRuntime` stays the only main brain; tools stay behind `ActionValidator -> ToolExecutor -> ToolRegistry`; memory/context stay behind their services; trace invariants become testable regression gates.

**Tech Stack:** Python, FastAPI, existing Gateway session manager/facade, `AgentGraphRuntime`, `TraceInvariantObserver`, pytest, static source checks with `ast` and `Path`.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest.
- Keep default paths mock/local/offline; do not call real providers.
- Use `apply_patch` for manual edits.
- Do not add dependencies.
- Do not perform large-scale refactors.
- Do not add a second Agent loop.
- Treat Phase 0 as an architecture gate, not an OS/platform build-out.
- Do not move planning, tool selection, memory policy, provider policy, or agent routing into Gateway or entry adapters.
- Do not route realtime Gateway turns directly into `AgentRouter`; realtime multi-agent work must enter `AgentGraphRuntime` first and delegate through the tool-governed boundary.
- Do not migrate legacy `/ws/agent/{session_id}`, CLI `--scenario`, or vendor `/agent-service/v1` in this phase; classify them explicitly.
- Do not enable `delegate_to_agent` in the default registry.
- Do not store API keys, raw provider payloads, raw memory content, raw prompts, hidden reasoning, base64, inline media bodies, or real user data in tests, traces, docs, or fixtures.
- Preserve existing public HTTP/WebSocket/CLI response schemas.

---

## Files To Inspect Before Execution

Run these read-only commands first:

```bash
rg -n "AssistantRuntimeApp|GatewayTurnFacade|GatewaySessionManager|GatewayAgentAdapter|AgentGraphRealtimeBackend|run_assistant_request|/agent/run|/agents/run|/ws/gateway|/ws/realtime/media|/ws/agent|/agent-service" src/assistant_agent tests scripts docs
rg -n "registry\.run\(|ToolExecutor\(|ActionValidator\(|TraceInvariantObserver|tool\.observation|action.validation.finished|run\.started|run\.completed|run\.failed|run\.cancelled" src/assistant_agent tests scripts docs
```

Expected current classification:

| Entry | Current path | Phase 0 classification |
| --- | --- | --- |
| HTTP `POST /agent/run` | `routes_agent.run_agent -> _run_agent_through_gateway -> GatewayTurnFacade -> GatewaySessionManager -> GatewayAgentAdapter -> AssistantRuntimeApp -> run_assistant_request -> AgentGraphRuntime` | canonical Gateway-first product entry |
| Gateway WS `/ws/gateway` | `gateway_websocket -> get_gateway_bridge().bridge(...) -> GatewaySessionManager` | canonical normalized Gateway entry |
| Realtime media WS `/ws/realtime/media` | media event validation -> Gateway frame mapper -> `get_gateway_bridge().bridge(...)` | canonical realtime entry adapter |
| Local CLI `--text` | local `GatewaySessionManager + GatewayTurnFacade + GatewayAgentAdapter` | canonical local Gateway-first entry |
| Legacy WS `/ws/agent/{session_id}` | local `GatewaySessionManager + GatewayTurnFacade + GatewayAgentAdapter`, with legacy `AgentEvent` stream mirroring | compatibility transport surface, Gateway-first internally, do not expand |
| CLI `--scenario` | demo matrix through local `GatewayTurnFacade` in `scripts/run_demo_flows.py` | offline demo adapter, Gateway-first internally, do not expand into product behavior |
| Vendor `/agent-service/v1` | vendor `message` / `sessionId` / stringified `body` protocol; `assistantControlStart` remains a handshake and `chat` uses local `GatewayTurnFacade` internally | compatibility vendor surface, Gateway-first internally |
| HTTP `POST /agents/run` | explicit `AgentRouter` debug/multi-agent entry | separate opt-in router entry, not default product path |
| Inbound A2A `/a2a/rpc` | protocol adapter over `AgentRouter` | explicit adapter, not Gateway lifecycle |
| MCP `tool_run` | `ActionValidator -> ToolExecutor -> ToolRegistry` | tool adapter path, not assistant entry |

---

### Task 1: Add Entry Convergence Contract Tests

**Files:**
- Create: `tests/test_phase0_entrypoint_contracts.py`
- Modify: `docs/gateway-architecture.md`

**Interfaces:**
- Consumes: source files under `src/assistant_agent/api/`, `scripts/run_assistant_cli.py`, and current Gateway architecture docs.
- Produces: static pytest checks that classify product entries, compatibility debt, and explicit router/tool adapters.

- [ ] **Step 1: Write the failing or confirming contract tests**

Create `tests/test_phase0_entrypoint_contracts.py`:

```python
import ast
from pathlib import Path


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _function_source(path: str, function_name: str) -> str:
    source = _source(path)
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"function {function_name} not found in {path}")


def test_http_agent_run_remains_gateway_first_product_entry() -> None:
    source = _function_source("src/assistant_agent/api/routes_agent.py", "run_agent")

    assert "_run_agent_through_gateway(request)" in source
    assert "GatewayTurnFacade" not in source
    assert "get_assistant_runtime_app().run_request" not in source
    assert "AgentGraphRuntime" not in source


def test_gateway_runtime_is_the_only_http_gateway_backend_capture_boundary() -> None:
    source = _source("src/assistant_agent/api/gateway_runtime.py")

    assert "GatewayAgentAdapter(run_request=_run_assistant_request_with_http_runtime)" in source
    assert "get_assistant_runtime_app().run_request(request, **kwargs)" in source
    assert "GATEWAY_HTTP_RESPONSE_CAPTURE_ID" in source
    assert "AgentGraphRuntime" not in source


def test_gateway_and_realtime_media_websockets_bridge_to_gateway_only() -> None:
    source = _source("src/assistant_agent/api/gateway_websocket.py")

    assert '@router.websocket("/ws/gateway")' in source
    assert '@router.websocket("/ws/realtime/media")' in source
    assert "get_gateway_bridge().bridge(" in source
    assert "get_assistant_runtime_app" not in source
    assert "run_assistant_request" not in source
    assert "AgentGraphRuntime" not in source


def test_local_cli_text_path_remains_gateway_first() -> None:
    source = _source("scripts/run_assistant_cli.py")

    assert "GatewaySessionManager(" in source
    assert "GatewayTurnFacade(manager=manager)" in source
    assert "GatewayAgentAdapter(" in source
    assert "run_demo_flows(scenario_id=args.scenario)" in source
    assert "AgentGraphRuntime(" not in source


def test_legacy_ws_agent_is_gateway_first_internally_but_keeps_legacy_event_surface() -> None:
    source = _source("src/assistant_agent/api/websocket.py")

    assert '@router.websocket("/ws/agent/{session_id}")' in source
    assert "GatewaySessionManager(" in source
    assert "GatewayAgentAdapter(run_request=run_request)" in source
    assert "GatewayTurnFacade(manager=manager)" in source
    assert "facade.run_turn(" in source
    assert "MirroringWebSocketEventSink(" in source
    assert "get_assistant_runtime_app().run_request(gateway_request, **kwargs)" in source
    assert "AgentGraphRuntime" not in source


def test_vendor_agent_service_v1_is_gateway_first_internally_but_keeps_vendor_surface() -> None:
    source = _source("src/assistant_agent/api/agent_service_websocket.py")

    assert '@router.websocket("/agent-service/{version}")' in source
    assert 'message_type = "assistantControlStart"' in source
    assert 'response_message = "assistantControlStartAck"' in source
    assert 'message_type = "chat"' in source
    assert 'response_message = "chatResponse"' in source
    assert "GatewaySessionManager(" in source
    assert "GatewayTurnFacade(manager=gateway_manager)" in source
    assert "GatewayAgentAdapter(" in source
    assert "state.gateway_facade.run_turn(" in source
    assert "get_assistant_runtime_app().run_request(request, **kwargs)" in source
    assert "AgentGraphRuntime" not in source


def test_agents_run_stays_explicit_agent_router_entry_not_default_gateway_path() -> None:
    source = _function_source("src/assistant_agent/api/routes_agent.py", "run_agents")

    assert "get_agent_router().run(request)" in source
    assert "_run_agent_through_gateway" not in source
    assert "GatewayTurnFacade" not in source
    assert "get_assistant_runtime_app().run_request" not in source


def test_demo_scenarios_are_gateway_first_offline_adapter() -> None:
    source = _source("scripts/run_demo_flows.py")

    assert "GatewaySessionManager(" in source
    assert "GatewayAgentAdapter(" in source
    assert "GatewayTurnFacade(manager=manager)" in source
    assert "_gateway_request_from_scenario(scenario)" in source
    assert "facade.run_turn(" in source
    assert "AgentGraphRuntime(" not in source
```

- [ ] **Step 2: Run the entry contract tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_entrypoint_contracts.py tests/test_architecture_boundaries.py -q
```

Expected: either PASS if the current source already matches the classification, or FAIL only where the current source has drifted from the intended boundary.

- [ ] **Step 3: If the tests fail, make the smallest source correction**

Expected minimal corrections:

- If `run_agent()` directly calls `AssistantRuntimeApp`, restore `_run_agent_through_gateway(request)`.
- If `/ws/gateway` or `/ws/realtime/media` imports runtime/app services directly, move that call back behind `get_gateway_bridge().bridge(...)`.
- If CLI `--text` constructs `AgentGraphRuntime` directly, restore the local `GatewaySessionManager -> GatewayTurnFacade -> GatewayAgentAdapter` path.
- If `/agent-service/v1` routes `chat` outside `GatewayTurnFacade`, restore the Gateway-first path while preserving the vendor envelope protocol.
- If legacy `/ws/agent/{session_id}` stops using `GatewayTurnFacade`, restore its internal Gateway path while preserving the legacy external `AgentEvent` stream.

- [ ] **Step 4: Document the entry inventory**

In `docs/gateway-architecture.md`, add a short "Entry Convergence Inventory" section after "Current Code Map" with the table from this task. The section must state that legacy `/ws/agent/{session_id}`, CLI `--scenario`, and vendor `/agent-service/v1` are Gateway-first internally but compatibility/demo/vendor surfaces externally, not places to add assistant behavior outside the Gateway path.

- [ ] **Step 5: Run Task 1 verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_entrypoint_contracts.py tests/test_architecture_boundaries.py tests/test_gateway_api.py tests/test_gateway_turn_facade.py tests/test_assistant_cli.py -q
git diff --check -- docs/gateway-architecture.md tests/test_phase0_entrypoint_contracts.py
```

Expected: all selected tests pass, and `git diff --check` reports no whitespace errors.

---

### Task 2: Add Tool Governance Contract Tests For Rejections

**Files:**
- Create: `tests/test_phase0_tool_governance_contracts.py`
- Modify: `src/assistant_agent/agent/runtime.py`

**Interfaces:**
- Consumes: `AgentGraphRuntime`, `ActionValidator`, `ToolExecutor`, `NativeToolCall`, `TraceInvariantObserver`.
- Produces: regression tests proving validation rejection does not allocate a tool call, does not emit `tool.started`, and still emits a traceable rejected observation in native runtime.

- [ ] **Step 1: Write the failing native validation-rejection test**

Create `tests/test_phase0_tool_governance_contracts.py`:

```python
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.hook_invariants import TraceInvariantObserver
from assistant_agent.services.trace_store import InMemoryTraceStore, trace_debug_summary


class ScriptedNativeChatAdapter:
    provider = "scripted-native"

    def __init__(self, outputs: list[ChatResult]) -> None:
        self.outputs = outputs
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


def test_native_validation_rejection_does_not_enter_tool_executor_and_is_observable() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="",
                provider="scripted",
                model="native-rejection-test",
                tool_calls=[
                    NativeToolCall(
                        id="call_rejected_1",
                        name="product_search",
                        arguments={},
                    )
                ],
                message_kind="tool_calls",
            )
        ]
    )
    runtime = AgentGraphRuntime(trace_store=trace_store, chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找鞋"))

    raw_events = trace_store.list_by_run(state.run_id)
    events = trace_debug_summary(raw_events)["events"]
    canonical = [event["canonical_event"] for event in events if event["canonical_event"]]
    validation = next(event for event in events if event["canonical_event"] == "action.validation.finished")
    observation = next(event for event in events if event["canonical_event"] == "tool.observation")

    assert state.tool_calls == []
    assert validation["status"] == "rejected"
    assert "tool.started" not in canonical
    assert "tool.finished" not in canonical
    assert "tool.failed" not in canonical
    assert observation["status"] == "rejected"
    assert observation["tool_name"] == "product_search"
    assert observation["error_code"] == "invalid_tool_input"
    assert TraceInvariantObserver(raw_events).is_valid() is True
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_tool_governance_contracts.py::test_native_validation_rejection_does_not_enter_tool_executor_and_is_observable -q
```

Expected: FAIL because native validation rejection does not currently emit canonical `tool.observation`.

- [ ] **Step 3: Add a native rejected-observation trace helper**

In `src/assistant_agent/agent/runtime.py`, add this helper near `_record_native_observation_metadata(...)`:

```python
def _append_native_tool_observation_event(runtime: AgentGraphRuntime, state: AgentState, observation: dict[str, Any]) -> None:
    runtime._append_observability_event(
        state,
        canonical_event="tool.observation",
        node_name="native_runtime",
        status=observation.get("status") if isinstance(observation, dict) else None,
        tool_name=observation.get("tool_name") if isinstance(observation, dict) else None,
        attributes={
            "summary": observation.get("summary") if isinstance(observation, dict) else None,
            "output_ref": observation.get("output_ref") if isinstance(observation, dict) else None,
            "next_step_hint": observation.get("next_step_hint") if isinstance(observation, dict) else None,
        },
        output_summary={
            "summary": observation.get("summary") if isinstance(observation, dict) else None,
            "output_ref": observation.get("output_ref") if isinstance(observation, dict) else None,
            "next_step_hint": observation.get("next_step_hint") if isinstance(observation, dict) else None,
        },
        error={
            "code": observation.get("error_code"),
            "message": observation.get("error_message"),
        }
        if isinstance(observation, dict) and observation.get("error_code")
        else None,
    )
```

Then replace the duplicated native accepted-tool `self._append_observability_event(... canonical_event="tool.observation" ...)` block with:

```python
_append_native_tool_observation_event(self, state, observation)
```

In the native validation rejection branch, immediately after `_record_native_observation_metadata(state, observation)`, add:

```python
_append_native_tool_observation_event(self, state, observation)
```

- [ ] **Step 4: Run the focused governance tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_tool_governance_contracts.py tests/test_tool_call_boundaries.py tests/test_observability_harness.py::test_native_runtime_emits_canonical_llm_decision_validation_observation_and_terminal_events -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Run ToolExecutor boundary verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_tool_executor.py tests/test_tool_call_boundaries.py tests/test_tool_risk_gate.py tests/test_provider_budget_in_tool_executor.py tests/test_mcp_server_skeleton.py tests/test_architecture_boundaries.py -q
```

Expected: all selected tests pass, and no API/WebSocket/MCP entry bypasses `ToolExecutor`.

---

### Task 3: Add Runtime Trace Invariant Gate Tests

**Files:**
- Create: `tests/test_phase0_trace_invariant_gate.py`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Consumes: `TraceInvariantObserver`, `AgentGraphRuntime`, `InMemoryTraceStore`, mock/offline runtime, scripted native runtime.
- Produces: a test gate proving representative mock and native traces satisfy the local invariant observer.

- [ ] **Step 1: Write the invariant gate tests**

Create `tests/test_phase0_trace_invariant_gate.py`:

```python
import pytest

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.hook_invariants import TraceInvariantObserver
from assistant_agent.services.trace_store import InMemoryTraceStore


class ScriptedNativeChatAdapter:
    provider = "scripted-native"

    def __init__(self, outputs: list[ChatResult]) -> None:
        self.outputs = outputs
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


def _assert_trace_invariants(trace_store: InMemoryTraceStore, run_id: str) -> None:
    observer = TraceInvariantObserver(trace_store.list_by_run(run_id))
    violations = observer.violations()
    assert violations == []


def test_mock_runtime_trace_satisfies_phase0_invariant_gate() -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找相似款"))

    _assert_trace_invariants(trace_store, state.run_id)


def test_native_runtime_trace_satisfies_phase0_invariant_gate() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                response_text="",
                provider="scripted",
                model="native-phase0-test",
                tool_calls=[
                    NativeToolCall(
                        id="call_native_1",
                        name="product_search",
                        arguments={"query": "白色运动鞋"},
                    )
                ],
                message_kind="tool_calls",
            ),
            ChatResult(
                response_text="找到了一些白色运动鞋。",
                provider="scripted",
                model="native-phase0-test",
                message_kind="content",
            ),
        ]
    )
    runtime = AgentGraphRuntime(trace_store=trace_store, chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找白色运动鞋"))

    _assert_trace_invariants(trace_store, state.run_id)


def test_phase0_invariant_gate_reports_broken_trace() -> None:
    from assistant_agent.services.trace_store import TraceEvent

    broken = TraceEvent(
        trace_id="trace_broken",
        run_id="run_broken",
        user_id="u1",
        session_id="s1",
        node_name="runtime",
        event_type="observability",
        canonical_event="run.started",
        status="started",
    )

    violations = TraceInvariantObserver([broken]).violations()

    assert [violation.code for violation in violations] == ["missing_run_terminal"]
```

- [ ] **Step 2: Run the invariant gate tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_trace_invariant_gate.py tests/test_hook_invariants.py tests/test_observability_harness.py -q
```

Expected: all selected tests pass after Task 2.

- [ ] **Step 3: Document the Phase 0 invariant gate**

In `docs/observability-harness.md`, under "Harness Invariants", add this sentence:

```markdown
Phase 0 architecture stabilization uses `tests/test_phase0_trace_invariant_gate.py`
as the local regression gate for representative mock/offline and native runtime
traces.
```

- [ ] **Step 4: Run documentation and trace verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_trace_invariant_gate.py tests/test_hook_invariants.py tests/test_observability_harness.py tests/test_trace_query_api.py tests/test_trace_redaction.py -q
git diff --check -- docs/observability-harness.md tests/test_phase0_trace_invariant_gate.py
```

Expected: all selected tests pass; trace/API summaries remain redacted.

---

### Task 4: Add Memory, Context, And Delegation Boundary Gate Tests

**Files:**
- Create: `tests/test_phase0_service_boundary_contracts.py`
- Modify: `docs/personal-realtime-ai-assistant-roadmap.md`

**Interfaces:**
- Consumes: source files and existing tests for memory/context/delegation boundaries.
- Produces: static tests that protect the architecture from moving memory retrieval, context rendering, or agent routing into the wrong layer.

- [ ] **Step 1: Write the service-boundary tests**

Create `tests/test_phase0_service_boundary_contracts.py`:

```python
from pathlib import Path


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_memory_tools_remain_thin_and_do_not_import_memory_stores() -> None:
    source = _source("src/assistant_agent/tools/memory_tool.py")

    assert "from assistant_agent.memory.store" not in source
    assert "from assistant_agent.memory.retrieval" not in source
    assert "from assistant_agent.memory.write_policy" not in source
    assert "MemoryManager" in source


def test_context_builder_does_not_own_memory_store_or_write_policy() -> None:
    context_paths = [
        "src/assistant_agent/services/context/builder.py",
        "src/assistant_agent/services/context/renderer.py",
        "src/assistant_agent/services/context/report.py",
    ]
    for path in context_paths:
        source = _source(path)
        assert "from assistant_agent.memory.store" not in source
        assert "from assistant_agent.memory.write_policy" not in source
        assert "MemoryStore" not in source


def test_agent_delegation_context_filters_parent_memory_and_history() -> None:
    source = _source("src/assistant_agent/services/agent_delegation_context.py")

    assert "memory_context_" in source
    assert "conversation_history" in source
    assert "omitted_context" in source
    assert "raw_provider" in source


def test_default_registry_does_not_enable_delegation_by_default() -> None:
    source = _source("src/assistant_agent/tools/registry.py")

    assert "enable_agent_delegation: bool = False" in source
    assert "delegate_to_agent" in source
```

- [ ] **Step 2: Run the service-boundary tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_service_boundary_contracts.py tests/test_memory_tool_boundary.py tests/test_memory_manager.py tests/test_assistant_context_renderer.py tests/test_agent_communication_routing.py tests/test_agent_router.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Mark Phase 0 gate in the roadmap**

In `docs/personal-realtime-ai-assistant-roadmap.md`, under "Phase 0 Gate", add these acceptance lines:

```markdown
- Product text/realtime entries have static contract tests proving Gateway-first routing.
- Tool governance has a rejection test proving invalid native tool calls do not enter `ToolExecutor`.
- Representative mock/offline and native traces pass `TraceInvariantObserver`.
- Memory, context, and delegation boundaries have regression tests that prevent obvious ownership drift.
```

- [ ] **Step 4: Run Task 4 verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_service_boundary_contracts.py tests/test_memory_tool_boundary.py tests/test_memory_manager.py tests/test_memory_read_policy.py tests/test_assistant_context_renderer.py tests/test_agent_communication_routing.py tests/test_agent_router.py -q
git diff --check -- docs/personal-realtime-ai-assistant-roadmap.md tests/test_phase0_service_boundary_contracts.py
```

Expected: all selected tests pass.

---

### Task 5: Add The Phase 0 Architecture Gate Command Set

**Files:**
- Modify: `docs/personal-realtime-ai-assistant-roadmap.md`
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: one documented command set that a single developer can run before moving to Phase 1 realtime voice work.

- [ ] **Step 1: Add the command set to the roadmap**

In `docs/personal-realtime-ai-assistant-roadmap.md`, add a "Phase 0 Architecture Gate Commands" subsection under "Phase 0 Gate":

````markdown
Run before starting Phase 1:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_entrypoint_contracts.py tests/test_architecture_boundaries.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_gateway_turn_facade.py tests/test_assistant_cli.py tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_tool_governance_contracts.py tests/test_tool_call_boundaries.py tests/test_tool_executor.py tests/test_tool_risk_gate.py tests/test_mcp_server_skeleton.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_trace_invariant_gate.py tests/test_hook_invariants.py tests/test_observability_harness.py tests/test_trace_query_api.py tests/test_trace_redaction.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_service_boundary_contracts.py tests/test_memory_tool_boundary.py tests/test_memory_manager.py tests/test_memory_read_policy.py tests/test_assistant_context_renderer.py tests/test_agent_communication_routing.py tests/test_agent_router.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- AGENTS.md docs src tests scripts
```
````

- [ ] **Step 2: Cross-link the gate from authority docs**

Add one sentence to each authority doc:

- `docs/gateway-architecture.md`: "Phase 0 entry convergence tests live in `tests/test_phase0_entrypoint_contracts.py`."
- `docs/tool-calling-architecture.md`: "Phase 0 tool governance rejection tests live in `tests/test_phase0_tool_governance_contracts.py`."
- `docs/observability-harness.md`: "Phase 0 trace invariant gate tests live in `tests/test_phase0_trace_invariant_gate.py`."

- [ ] **Step 3: Run final Phase 0 gate**

Run the full command set added in Step 1.

Expected:

- `scripts/check_env.py` exits 0.
- All targeted pytest commands pass.
- `pytest -m fast -q` passes.
- `git diff --check -- AGENTS.md docs src tests scripts` reports no whitespace errors.
- No command requires real provider keys or network access.

- [ ] **Step 4: Commit**

Use a single commit for Phase 0 stabilization:

```bash
git add docs/gateway-architecture.md docs/tool-calling-architecture.md docs/observability-harness.md docs/personal-realtime-ai-assistant-roadmap.md src/assistant_agent/agent/runtime.py tests/test_phase0_entrypoint_contracts.py tests/test_phase0_tool_governance_contracts.py tests/test_phase0_trace_invariant_gate.py tests/test_phase0_service_boundary_contracts.py
git commit -m "test: add phase 0 architecture gates"
```

---

## Scope Exclusions

- Do not implement durable session/run store in Phase 0. Write a separate design plan after the contract gates pass.
- Do not migrate legacy `/ws/agent/{session_id}` in Phase 0. It needs a focused compatibility migration plan because its public event schema differs from Gateway frames.
- Do not migrate CLI `--scenario` in Phase 0. It is demo-matrix behavior, not the Personal Realtime Assistant runtime path.
- Do not further expand `/agent-service/v1` in Phase 0. It is a vendor compatibility surface whose `chat` path must remain behind `GatewayTurnFacade`.
- Do not add dashboards, exporters, OpenTelemetry, background workers, vector memory, skill installation, or realtime ASR/TTS in Phase 0.
- Do not add RL pipelines, trajectory stores, skill marketplaces, user-uploaded skills, multi-agent fabric, OS control planes, scheduler systems, or background autonomous processes in Phase 0.

## Self-Review

- Spec coverage: the plan covers entry inspection, contract tests, trace invariants, memory/context/delegation boundary checks, and exact acceptance commands.
- Completeness scan: no unresolved work markers remain.
- Type consistency: all referenced test names, files, and public classes exist or are created in a preceding task.
- Risk check: the only runtime behavior change is a narrow native-runtime trace event for rejected tool observations; no user-facing API schema or tool execution behavior changes.
- Phase dependency check: after this plan passes, Phase 1 realtime voice can build on stable Gateway, runtime, tool, memory, context, and trace boundaries.

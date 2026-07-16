# Phase 4 Multi-Agent Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Phase 4 readiness gate for multi-agent delegation without turning on a full agent fabric.

**Architecture:** Keep `/agent/run` and default Gateway/runtime behavior single-agent. Keep `/agents/run`, `delegate_to_agent`, `AgentRouter`, and outbound A2A explicitly opt-in. Add a narrow JSONL-backed `AgentControlPlaneStore` implementation so delegation route records and audit events can survive process restart when a caller explicitly provides that store.

**Tech Stack:** Python 3, Pydantic v2, pytest, local JSONL persistence, existing `AgentRouter` / `AgentControlPlaneStore` / `AgentControlPlaneQueryService`.

## Global Constraints

- Default mock/local/offline behavior only.
- No new dependencies.
- No real provider calls.
- No remote agent discovery, agent swarm, marketplace, or default multi-agent enablement.
- Child agent memory/context isolation remains owned by `DelegationContextBuilder`.
- All delegation remains behind `delegate_to_agent -> ActionValidator -> ToolExecutor -> ToolRegistry -> AgentCommunicationService`.

---

### Task 1: Durable Control-Plane Store Contract

**Files:**
- Modify: `src/assistant_agent/services/agent_control_plane.py`
- Test: `tests/test_phase4_multi_agent_readiness_gate.py`

**Interfaces:**
- Consumes: `AgentControlPlaneRunRecord`, `AgentAuditEvent`, `AgentControlPlaneStore`
- Produces: `JsonlAgentControlPlaneStore(path: Path | str)` with existing store methods and a `retention()` method returning durable metadata.

- [x] **Step 1: Write the failing durable-store test**

Create `tests/test_phase4_multi_agent_readiness_gate.py` with a test that:

```python
def test_jsonl_control_plane_store_survives_restart_and_preserves_delegation_trace(tmp_path):
    ...
```

The test must instantiate an `AgentRouter` with `JsonlAgentControlPlaneStore(tmp_path / "agent_control_plane.jsonl")`, run a `controller_delegate` request through a `RecordingRuntime` that emits one `delegate_to_agent` tool result, then instantiate a second store from the same path and assert:

- `get(run_id)` returns the redacted route record.
- `get_by_trace_id(trace_id)` returns the same run record.
- `delegated_tasks[0]["target_agent_id"] == WORKER_AGENT_ID`.
- persisted audit events include `route_decision` and `delegation_decision`.
- serialized persisted output does not include raw parent memory, raw provider response, or secret-like text.
- `AgentControlPlaneQueryService(...).audit_events_by_run(run_id).retention["durable"] is True`.

- [x] **Step 2: Verify the test fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase4_multi_agent_readiness_gate.py -q
```

Expected: FAIL because `JsonlAgentControlPlaneStore` is not implemented.

- [x] **Step 3: Implement the minimal JSONL store**

Add `JsonlAgentControlPlaneStore` to `agent_control_plane.py`.

Implementation requirements:

- Store records and audit events in one JSONL file using an envelope:
  - `{"kind": "run_record", "payload": ...}`
  - `{"kind": "audit_event", "payload": ...}`
- Use `model_dump(mode="json")` when writing.
- Use `AgentControlPlaneRunRecord.model_validate(...)` and `AgentAuditEvent.model_validate(...)` when reading.
- Create parent directories.
- Keep append-only writes.
- On read, latest record for a `run_id` wins.
- Implement `retention()` returning:

```python
{
    "storage": "jsonl_file",
    "durable": True,
    "retention_policy": "explicit_local_file_until_deleted",
    "phase": "phase4_multi_agent_readiness",
}
```

- [x] **Step 4: Run the gate test to green**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase4_multi_agent_readiness_gate.py -q
```

Expected: PASS.

---

### Task 2: Preserve Default Process-Local Behavior

**Files:**
- Modify: `src/assistant_agent/services/agent_control_plane.py`
- Test: `tests/test_phase4_multi_agent_readiness_gate.py`

**Interfaces:**
- Consumes: `InMemoryAgentControlPlaneStore`, `AgentControlPlaneQueryService`
- Produces: store-specific retention reporting without changing default router behavior.

- [x] **Step 1: Write the default-retention test**

Add a test that instantiates the default `AgentRouter` without passing a store and asserts:

- `router.control_plane_store` is still `InMemoryAgentControlPlaneStore`.
- `AgentControlPlaneQueryService(..., router_store=router.control_plane_store).audit_events_by_run(run_id).retention["durable"] is False`.
- retention storage is `process_local_memory`.

- [x] **Step 2: Verify the test fails or passes for the right reason**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase4_multi_agent_readiness_gate.py -q
```

Expected before implementation: default behavior may pass, but durable store retention should fail until Task 1 code exists. If the default test passes immediately, keep it as a regression guard.

- [x] **Step 3: Add store-specific retention lookup**

Update `InMemoryAgentControlPlaneStore` with:

```python
def retention(self) -> dict[str, Any]:
    return dict(AUDIT_RETENTION)
```

Update `AgentControlPlaneQueryService.audit_events(...)` to call `router_store.retention()` when available and fall back to `AUDIT_RETENTION`.

- [x] **Step 4: Run the focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase4_multi_agent_readiness_gate.py tests/test_api_agent_graph_runtime.py::test_control_plane_api_queries_agent_router_run -q
```

Expected: PASS.

---

### Task 3: Documentation and Roadmap Gate

**Files:**
- Modify: `docs/agent-communication-routing.md`
- Modify: `docs/roadmaps/personal-realtime-ai-assistant-roadmap.md`

**Interfaces:**
- Consumes: Phase 4 roadmap gate and existing multi-agent authority.
- Produces: a clear statement that Phase 4 is readiness-only and opt-in durable trace exists without product fabric expansion.

- [x] **Step 1: Update multi-agent authority**

In `docs/agent-communication-routing.md`, update current status and routing rules to say:

- Default store remains process-local.
- `JsonlAgentControlPlaneStore` provides explicit local durable delegation trace for pilot/readiness use.
- Durable store does not enable default multi-agent routing, remote discovery, or remote A2A.

- [x] **Step 2: Update roadmap Phase 4 gate**

In `docs/roadmaps/personal-realtime-ai-assistant-roadmap.md`, update Phase 4 Gate:

- child agent raw parent memory/context isolation covered by delegation context tests.
- durable delegation trace covered by explicit JSONL control-plane store tests.
- repeated-pair and depth control covered by delegation policy tests.
- Phase 4 remains not a default fabric/product rollout.

- [x] **Step 3: Run doc diff check**

Run:

```bash
git diff --check -- docs/agent-communication-routing.md docs/roadmaps/personal-realtime-ai-assistant-roadmap.md
```

Expected: no output and exit code 0.

---

### Task 4: Verification and Commit

**Files:**
- All modified Phase 4 files.

**Interfaces:**
- Consumes: previous tasks.
- Produces: committed Phase 4 readiness patch.

- [x] **Step 1: Run Phase 4 gate**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase4_multi_agent_readiness_gate.py -q
```

- [x] **Step 2: Run multi-agent regression suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_communication_routing.py tests/test_agent_router.py tests/test_agent_routing_policy.py tests/test_api_a2a.py tests/test_a2a_json_rpc_transport.py tests/test_agent_pilot_readiness.py tests/test_api_agent_graph_runtime.py -q
```

- [x] **Step 3: Run fast suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

- [x] **Step 4: Run diff check**

```bash
git diff --check -- AGENTS.md docs src tests scripts skills
```

- [x] **Step 5: Commit**

```bash
git add docs/agent-communication-routing.md docs/roadmaps/personal-realtime-ai-assistant-roadmap.md docs/superpowers/plans/2026-07-09-phase-4-multi-agent-readiness.md src/assistant_agent/services/agent_control_plane.py tests/test_phase4_multi_agent_readiness_gate.py
git commit -m "参考hermes的长期个人助手:phase4"
```

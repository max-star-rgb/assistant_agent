# Unified Tool Policy Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents unless the user explicitly authorizes delegation.

**Goal:** Make the existing `ToolSpec` governance declarations enforceable and entry-independent while preserving the provider-native assistant loop and `ActionValidator -> ToolExecutor -> ToolRegistry` boundary.

**Architecture:** Keep `ToolSpec` backward compatible and compile it into one immutable `ToolPolicyView`. Resolve that view again from `ToolRegistry` at the execution boundary, then use it for risk/approval/idempotency, safe retry, deadline propagation, observation limits, and trace redaction. Capability qualification and realtime commit lifecycle remain separate concerns.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, existing mock/local/scripted providers.

## Constraints

- Do not move the agent loop into Gateway and do not add a JSON controller.
- Do not change provider-native tool calling or expose internal policy metadata in provider schemas.
- Do not merge this work with the current capability qualification/identity recall changes.
- Do not call real providers or install dependencies.
- Preserve existing local user changes and edit overlapping files narrowly.
- Use test-first changes for each behavior segment.

## File Map

- `src/assistant_agent/services/tool_policy.py`: canonical static policy interpretation.
- `src/assistant_agent/services/tool_risk_gate.py`: dynamic confirmation and idempotency decisions.
- `src/assistant_agent/tools/registry.py`: single-tool `ToolSpec` lookup using the same spec builder as inventory generation.
- `src/assistant_agent/agent/tool_executor.py`: execution-time policy enforcement, safe retry, deadline propagation, and trace/history handling.
- `src/assistant_agent/schemas/tool_observation.py`: bounded observation contract.
- `src/assistant_agent/services/context/compaction.py`: context-size enforcement.
- `src/assistant_agent/agent/runtime.py` and `src/assistant_agent/agent/assistant_loop_nodes.py`: pass registry-owned observation limits into the LLM observation boundary.
- `src/assistant_agent/services/tool_call_boundary.py`: policy-consistent trace summaries.
- `tests/**`: policy parity, retry, deadline, observation, redaction, cancellation, and provider-schema regression coverage.
- `docs/tool-calling-architecture.md`: authoritative current behavior after code is verified.

---

### Task 1: Establish Baseline and Canonical Policy View

**Files:**
- Modify: `tests/unit/test_tool_policy_metadata.py`
- Modify: `tests/test_tool_registry.py` or the nearest existing registry test file
- Modify: `src/assistant_agent/services/tool_policy.py`
- Modify: `src/assistant_agent/tools/registry.py`

- [ ] **Step 1: Run the focused baseline**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_calendar_create_event_slice.py \
  tests/test_tool_risk_gate.py \
  tests/unit/test_tool_policy_metadata.py \
  tests/test_tool_policy_parity_integration.py
```

Record existing failures; do not treat the known calendar confirmation/idempotency failures as new regressions.

- [ ] **Step 2: Add failing policy-view and registry tests**

Cover:

- rich `RealtimeToolPolicy.interruptible` and `commit_boundary` appear in `ToolPolicyView`;
- legacy specs have conservative/empty realtime defaults;
- `ToolRegistry.get_spec(name)` returns the same contract as the corresponding `list_specs()` item;
- unknown names raise the registry's existing not-found error rather than inventing a policy.

- [ ] **Step 3: Verify RED**

Run only the new tests and confirm failure because the view fields and `get_spec()` do not exist.

- [ ] **Step 4: Implement the minimal canonical interpretation**

- Move pure risk-level and confirmation-owner interpretation into `tool_policy.py` to remove its reverse dependency on `tool_risk_gate.py`.
- Add `interruptible` and `commit_boundary` to `ToolPolicyView`.
- Preserve legacy/rich precedence from the approved design.
- Add one private registry spec builder used by both `get_spec()` and `list_specs()`.
- Keep compatibility exports from `tool_risk_gate.py` where existing imports depend on them.

- [ ] **Step 5: Verify GREEN**

Run the focused policy and registry tests.

---

### Task 2: Enforce Approval and Idempotency from ToolPolicyView

**Files:**
- Modify: `tests/test_tool_risk_gate.py`
- Modify: `tests/test_calendar_create_event_slice.py` only if additional assertions are needed
- Modify: `tests/test_tool_policy_parity_integration.py`
- Modify: `src/assistant_agent/services/tool_risk_gate.py`
- Modify: `src/assistant_agent/agent/tool_executor.py`

- [ ] **Step 1: Add failing entry-parity tests**

Cover:

- a rich `external_write + approval=always + idempotency=required` tool requests confirmation in a normal non-realtime call;
- realtime and normal calls make the same base approval decision;
- confirmed calls without an idempotency key remain blocked;
- confirmed calls with a key execute once and a duplicate key is suppressed;
- model arguments cannot forge confirmation evidence;
- rich read-only tools execute without an added confirmation round.

- [ ] **Step 2: Verify RED**

Run the focused risk/calendar/parity tests. Confirm that current execution either bypasses ordinary-call confirmation or drops rich idempotency facts.

- [ ] **Step 3: Implement policy-view enforcement**

- Resolve `ToolRegistry.get_spec(tool_name)` and `ToolPolicyInterpreter.view_for_spec(spec)` at the start of `ToolExecutor.run_tool()`.
- Pass the full view to `evaluate_tool_risk()`.
- Make a tool's own approval requirement independent of request source/realtime metadata; entry metadata may only tighten behavior.
- Preserve tool-owned confirmation behavior and current process-local idempotency ledger.
- Remove the executor helper that downgrades a spec to `ToolSideEffectPolicy`.
- Ensure blocked, pending, cancelled, and failed calls are not committed to the ledger.

- [ ] **Step 4: Verify GREEN and regression safety**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_calendar_create_event_slice.py \
  tests/test_tool_risk_gate.py \
  tests/test_tool_policy_parity_integration.py \
  tests/test_tool_executor.py
```

---

### Task 3: Add Safe Retry and Deadline Propagation

**Files:**
- Modify: `tests/test_retry_policy.py`
- Modify: `tests/test_tool_executor.py`
- Modify: `src/assistant_agent/agent/tool_executor.py`
- Modify provider/service adapters only where an existing timeout/deadline seam already exists.

- [ ] **Step 1: Add failing retry tests**

Cover:

- rich read-only retry count is `min(tool.retry_count, global.max_retries)`;
- rich `retry_count=0` disables retries;
- non-idempotent mutating tools do not retry retryable provider failures;
- idempotency-protected mutation may retry within both limits;
- legacy read-only tools retain the current global retry behavior.

- [ ] **Step 2: Add failing deadline tests**

Verify that rich `timeout_s` appears in trusted `ToolContext.metadata` as:

```text
tool_execution.timeout_s
tool_execution.deadline_monotonic_s
```

Also verify trace distinguishes declared/propagated deadline from adapter-reported enforcement.

- [ ] **Step 3: Verify RED**

Run the focused retry and executor cases.

- [ ] **Step 4: Implement the minimal replay-safety calculation**

- A call is replay-safe when it is read-only, or when idempotency is required and the current risk decision has a key.
- For rich policy use `min(tool.retry_count, global.max_retries)`; for legacy read-only policy preserve the current global limit.
- Do not automatically replay non-idempotent mutation.
- Propagate a process-local monotonic deadline; do not attempt to kill synchronous Python threads.
- Record whether deadline enforcement was merely propagated or explicitly reported by the adapter.

- [ ] **Step 5: Verify GREEN**

Run retry, executor, cancellation, and calendar tests.

---

### Task 4: Bound LLM Observations and Redact Policy-Sensitive Traces

**Files:**
- Modify: `tests/test_tool_observation.py` or the nearest observation/compaction tests
- Modify: `tests/test_tool_call_boundary.py`
- Modify: `src/assistant_agent/schemas/tool_observation.py`
- Modify: `src/assistant_agent/services/context/compaction.py`
- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `src/assistant_agent/agent/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/services/tool_call_boundary.py`
- Modify: `src/assistant_agent/agent/tool_executor.py` only for trace/history-safe summaries.

- [ ] **Step 1: Add failing observation-limit tests**

Cover:

- `max_result_chars` bounds only the `ToolObservation` sent to the next LLM round;
- status, summary, error code, output reference, original size, and `truncated=true` survive;
- `raw_data_ref` is not copied into provider prompt content;
- provider-returned data cannot increase the registry-owned limit.

- [ ] **Step 2: Add failing redaction tests**

For `redact_in_trace=true`, assert trace/history contains safe keys, sizes, status, and references but not private input values or raw provider payloads. Confirm provider/MCP schemas still omit internal policy metadata.

- [ ] **Step 3: Verify RED**

Run only the new observation and trace cases.

- [ ] **Step 4: Implement bounded observation and trace-safe summaries**

- Pass `max_result_chars` from the registry-owned policy view into `observation_from_tool_result()` or the existing compaction seam.
- Add explicit truncation metadata without putting internal policy into `ToolResult.data`.
- Apply `DataPolicy.redact_in_trace` only at history/trace boundaries; do not turn it into a new authorization framework.
- Preserve audit/output references and keep raw references out of LLM observations.

- [ ] **Step 5: Verify GREEN**

Run observation, context compaction, tool boundary, native handoff, and provider-schema tests.

---

### Task 5: Documentation, Full Regression, and Self-Review

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Modify tests or implementation only for defects exposed by verification.

- [ ] **Step 1: Update current architecture documentation**

Document only implemented behavior:

- registered spec versus run-visible capability remains a separate concern;
- `ToolPolicyView` is the execution interpretation;
- approval/idempotency and safe retry semantics;
- deadline propagation limitations;
- LLM observation limit and trace redaction boundary;
- explicitly deferred realtime commit lifecycle and distributed ledger.

- [ ] **Step 2: Run focused regression suites**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_calendar_create_event_slice.py \
  tests/unit/test_tool_policy_metadata.py \
  tests/test_tool_policy_parity_integration.py \
  tests/test_tool_risk_gate.py \
  tests/test_retry_policy.py \
  tests/test_tool_executor.py \
  tests/test_native_tool_call_handoff.py \
  tests/test_realtime_turn_cancellation.py
```

- [ ] **Step 3: Run broader fast regression**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

If marker coverage is insufficient, run the full suite or the affected package tests explicitly.

- [ ] **Step 4: Perform a final self-review**

Check:

- no path bypasses `ActionValidator -> ToolExecutor -> ToolRegistry`;
- no Gateway or provider adapter became the policy authority;
- provider schemas do not expose internal policy fields;
- failed/pending/cancelled calls do not commit idempotency records;
- retries cannot replay unsafe mutation;
- deadline propagation is not described as forced cancellation;
- existing capability qualification changes remain intact;
- no new speculative taxonomy or realtime commit protocol was introduced.

- [ ] **Step 5: Report results**

Summarize implemented behavior, changed files, exact test results, limitations, and the next independent design item. Do not claim completion without fresh verification evidence.

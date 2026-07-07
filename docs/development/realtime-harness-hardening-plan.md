# Realtime Harness Hardening Plan

Last updated: 2026-07-06

This is the active development plan for hardening realtime call behavior as a
runtime harness system. It is not the architecture authority. The architecture
authorities remain:

- `docs/gateway-architecture.md` for Gateway, realtime frames, session/run,
  cancel, interrupt, reconnect, hangup, and stream lifecycle boundaries.
- `docs/tool-calling-architecture.md` for `ToolSpec`, `ActionValidator`,
  `ToolExecutor`, tool observations, side-effect policy, provider-native tool
  calls, retry, recovery, and tool governance.
- `docs/CONTEXT_ENGINEERING_STATUS.md` for assistant context assembly,
  realtime task-state snapshots, tool observation compaction, context budget,
  and current context-engineering status.

This plan coordinates medium-term work across those boundaries. When a phase
changes an actual boundary or durable behavior, update the relevant authority
document in the same change.

## Purpose

Realtime call assistants should not be treated as only:

```text
LLM + WebSocket + TTS
```

The durable target is a realtime harness that can absorb latency, interruption,
tool uncertainty, side effects, state recovery, and user attention limits while
keeping the assistant runtime truthful and deterministic where it matters.

Target behavior:

- The user hears an immediate, short, deterministic acknowledgement when model
  or tool latency exceeds realtime thresholds.
- Long or blocked work continues behind the runtime boundary without relying on
  the first model token to preserve user trust.
- Interrupts cancel or gate stale output quickly, preserve useful state, and
  avoid claiming that committed side effects were undone.
- The assistant receives a structured realtime state snapshot, not an
  unbounded transcript dump.
- Tools keep running through validator, executor, registry, policy, audit,
  retry, recovery, and trace boundaries.
- Side-effecting tools have risk metadata, confirmation behavior, and
  idempotency protection before irreversible execution is allowed.

## Scope

In scope:

- Realtime latency fallbacks and replaceable progress/TTS messages.
- Realtime call state and reducer-style updates behind the existing Gateway and
  assistant runtime boundaries.
- Explicit pre-tool and post-tool governance points.
- Tool side-effect gate hardening and idempotency records.
- Lightweight plan/checkpoint behavior only for long, multi-step, or
  recoverable realtime tasks.
- Offline deterministic tests for Gateway, realtime backend, context, task
  state, validator, executor, and event mapping behavior.

Out of scope:

- Memory-system redesign or memory write-policy changes. Memory has a separate
  owner and roadmap.
- A full DAG planner for all realtime requests.
- A second assistant loop, OpenClaw-style runtime, or worker-agent Gateway frame
  dependency.
- Real external telephony, TTS, STT, notification, payment, order, database, or
  other side-effect providers unless a later explicitly opt-in pilot phase adds
  them.
- Provider token streaming as a prerequisite. Streaming can improve UX, but the
  harness must still provide deterministic fallback when streaming is absent or
  delayed.

## Current Baseline

Already implemented foundations:

- Gateway owns normalized realtime session/run/cancel/interrupt/reconnect/
  hangup and stream-frame semantics.
- `assistant_agent.realtime` is a thin adapter boundary into the current
  `AgentGraphRuntime` / assistant loop.
- `RealtimeCancelToken` is passed into the assistant runtime and tool executor.
- Gateway suppresses stale outbound frames after cancel, interrupt, or deadline
  cancellation.
- Provider-native tool calls are normalized to internal
  `AssistantDecision(type="tool_call")` and must pass through
  `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`.
- Native tool calls can emit replaceable `progress_message` events such as
  "I will check that." before the final answer is generated.
- `RealtimeTaskState` records objective, revisions, reusable artifacts,
  side-effect records, and continuation strategy for realtime turns.
- `RealtimeTaskState` now also carries prompt-safe realtime call-state fields
  for pending tool wait, display/TTS state, last spoken progress, speech turn
  id, barge-in source, and bounded realtime event ids.
- Context engineering injects prompt-safe realtime task-state snapshots and
  compacted tool observations.
- `ToolSpec.side_effect` classifies tool side effects, and realtime task-state
  consumes that metadata for interrupt continuation strategy.

Known gaps:

- Replaceable tool progress currently depends on the model first returning a
  tool call. There is no realtime SLA fallback that fires before the model or
  tool path produces an event.
- Realtime task state does not yet model TTS state, last spoken progress,
  pending tool wait state, speech turn ids, or barge-in source explicitly.
- Pre-tool and post-tool behavior exists across validator, executor, trace, and
  task-state code, but it is not expressed as a clear reusable hook boundary.
- General pre-execution confirmation gating is not yet wired into
  `ActionValidator` / `ToolExecutor`.
- Side-effecting tools do not yet have a common `idempotency_key` contract or
  execution ledger.
- Checkpoint/resume is represented as a strategy, but durable checkpoint
  records and selection criteria are not implemented.

## Relationship To Existing Development Plans

Keep these documents as phase records, not as competing architecture entries:

- `docs/development/realtime_phone_backend_plan.md`: original neutral realtime
  backend plan and ownership boundary.
- `docs/development/realtime-agent-interrupt-phase2-plan.md`: implemented
  cooperative cancellation, deadline, and stale event suppression phases.
- `docs/development/realtime-agent-task-state-plan.md`: task-state,
  interruption revision, reusable artifact, and side-effect continuation work.
- `docs/development/gateway-entry-layer-development-plan.md`: implemented
  Gateway entry-layer route and media-entry adapter work.

This hardening plan is the umbrella roadmap for the next realtime harness
round. Detailed implementation phases may still land in focused documents when
they become large enough, but this file should keep the current priority order
and completion status visible.

## Design Principles

- Runtime owns deterministic safety and timing behavior. Do not rely on the
  model to always produce the first user-facing acknowledgement quickly enough.
- Gateway owns protocol and lifecycle. It should cancel, gate, suppress, and
  forward frames, but not become the semantic planner.
- The realtime adapter remains thin. It maps requests, events, results, and
  cancellation; it should not own tool choice, provider policy, memory policy,
  or multi-agent routing.
- Assistant runtime owns reasoning and tool orchestration behind the existing
  `AgentGraphRuntime` / assistant loop.
- Tool governance remains mandatory. No realtime shortcut may bypass
  `ActionValidator`, `ToolExecutor`, `ToolRegistry`, policy, audit, retry,
  recovery, or trace.
- State should be structured before it is injected into model context. Prefer a
  prompt-safe snapshot over raw event logs.
- Progress and fallback messages are display/TTS events, not durable facts.
  They should not be written into LLM message history as assistant answers.
- Use lightweight plan/checkpoint only when the task needs recovery,
  sequencing, or cross-tool continuity.

## Phase 0: Baseline Lock And Metrics

Goal: make the current realtime harness behavior measurable before adding new
fallbacks.

Expected work:

- Confirm current Gateway, realtime backend, task-state, and event-mapping test
  coverage.
- Add or update trace/debug metadata for:
  - request received time
  - first backend event time
  - first user-visible progress time
  - first response chunk time
  - first final answer time
  - cancel/interrupt observed time
  - stale event suppression count
- Keep metrics prompt-safe and free of raw provider payloads.

Acceptance:

- A local test can assert first-progress timing metadata without real provider
  calls.
- Existing realtime tests continue to pass.
- Metrics do not change public Gateway frame names.

Candidate files:

- `src/assistant_agent/realtime/agent_graph_backend.py`
- `src/assistant_agent/realtime/event_mapping.py`
- `src/assistant_agent/gateway/session.py`
- `tests/test_realtime_agent_backend.py`
- `tests/test_realtime_event_mapping.py`
- `tests/test_gateway_session.py`

## Phase 1: Deterministic Realtime Fallback Messages

Goal: emit a short, deterministic, replaceable progress/TTS message when model
or tool latency exceeds a realtime threshold.

Expected behavior:

- If a realtime run has no user-visible backend event within a configured
  threshold, emit a display-only `run.progress` event with a short message.
- If the model later emits a native tool-call `progress_message`, tool progress,
  response chunk, or final answer, the UI/TTS layer can replace or supersede the
  fallback.
- The fallback is not added to LLM messages and is not treated as final answer
  content.
- Fallback policy is enabled for realtime/Gateway requests only, not ordinary
  `/agent/run` by default.

Initial fallback messages:

- General pending run: `I am on it.`
- Tool or search likely pending: `I will check that.`
- Interrupt revision: `Got it. I am updating that now.`
- Confirmation wait: `I need to confirm that before doing it.`

Implementation notes:

- Prefer adding the SLA timer in the realtime adapter or Gateway session layer,
  where user-visible event timing is known.
- Keep existing native tool-call `progress_message` behavior in
  `AgentGraphRuntime`; do not move native tool handling into Gateway.
- Mark fallback payloads with `display_only=true`, `replaceable=true`,
  `source="realtime_sla_fallback"`, and a policy/version field.

Acceptance:

- A realtime run with delayed backend output emits one fallback progress event.
- A realtime run with quick response output emits no fallback.
- A fallback is suppressed after cancel/hangup.
- A later final response still arrives normally.
- The fallback does not appear in assistant message history or final answer
  text.

Candidate files:

- `src/assistant_agent/realtime/agent_graph_backend.py`
- `src/assistant_agent/realtime/progress.py`
- `src/assistant_agent/realtime/event_mapping.py`
- `src/assistant_agent/gateway/session.py`
- `tests/test_realtime_agent_backend.py`
- `tests/test_realtime_event_mapping.py`
- `tests/test_gateway_session.py`

## Phase 2: Realtime Call State Reducer Extensions

Goal: extend existing `RealtimeTaskState` into a more complete realtime call
state snapshot without creating a second state authority.

Expected state additions:

- `pending_tool`: prompt-safe summary of the current or most recent tool wait.
- `tts_state`: current TTS/display state such as `idle`, `speaking`,
  `interrupted`, or `superseded`.
- `last_spoken_progress`: last display/TTS progress message id and short text.
- `speech_turn_id`: stable id for the current speech/transcript turn.
- `barge_in_source`: whether the latest interrupt came from transcript,
  explicit cancel, hangup, or media relay control.
- `last_realtime_event_ids`: bounded provenance for dedupe and trace.

Implementation notes:

- Add reducer-style helper functions that accept current state plus runtime
  events and return updated state.
- Keep snapshots prompt-safe. Do not store raw audio, raw transcript streams,
  raw provider payloads, or TTS binary data.
- Continue injecting concise text through context engineering rather than
  putting raw event logs into conversation history.

Acceptance:

- Tests cover first turn, progress fallback, tool start/finish, interrupt,
  stale event suppression, and hangup state transitions.
- Prompt snapshot stays bounded and contains only user-relevant state.
- Existing task-state tests still pass.

Candidate files:

- `src/assistant_agent/services/realtime_task_state.py`
- `src/assistant_agent/services/context/renderer.py`
- `src/assistant_agent/services/assistant_run_service.py`
- `tests/test_realtime_task_state.py`
- `tests/test_assistant_context_renderer.py`
- `tests/test_shared_assistant_run_service.py`

## Phase 3: Explicit PreToolCall And PostToolCall Boundaries

Goal: make realtime tool governance easier to evolve without scattering
conditionals across runtime, validator, executor, and task-state code.

Expected behavior:

- `PreToolCall` runs before tool execution and can inspect:
  - tool name and normalized input
  - runtime identity
  - side-effect policy
  - realtime task state snapshot
  - cancel/deadline state
  - confirmation requirements
  - idempotency metadata
- `PostToolCall` runs after success, failure, cancellation, or confirmation
  pending and can update:
  - prompt-safe tool observation summary
  - reusable artifact records
  - side-effect records
  - pending confirmation records
  - progress/trace metadata

Implementation notes:

- This does not need a large plugin framework. Start with small service
  functions or a focused class called by existing `ActionValidator` and
  `ToolExecutor` boundaries.
- Preserve `ActionValidator` as the execution-eligibility gate and
  `ToolExecutor` as the only registry execution path.
- Keep memory-specific behavior out of this phase except for preserving
  existing memory tool contracts.

Acceptance:

- Existing validator/executor tests continue to pass.
- Realtime-specific tool policy can be tested without invoking a real provider.
- Tool rejection, tool success, tool failure, and cancellation all produce
  expected structured metadata.

Candidate files:

- `src/assistant_agent/agent/action_validator.py`
- `src/assistant_agent/agent/tool_executor.py`
- `src/assistant_agent/services/realtime_task_state.py`
- `src/assistant_agent/tools/registry.py`
- `tests/test_action_validator.py`
- `tests/test_tool_executor.py`
- `tests/test_realtime_task_state.py`

## Phase 4: Risk Gate And Idempotency Ledger

Goal: prevent duplicate or unsafe side-effecting tool execution in realtime
flows with retries, interrupts, reconnects, or repeated user utterances.

Risk mapping:

- `auto`: read-only or local-safe tools such as local memory retrieval,
  product search, price compare, image/video understanding.
- `soft_gate`: reversible, compensatable, or draft-producing tools such as
  image generation, 3D render, and local agent delegation.
- `hard_gate`: irreversible or externally visible actions such as sending,
  paying, deleting, changing account/device configuration, or committing an
  external order.
- `block`: disallowed or policy-violating actions.

Implementation notes:

- Map these runtime gate levels onto existing `ToolSpec.side_effect` rather
  than replacing that schema.
- Add an optional `idempotency_key` contract for side-effecting tools.
- Generate a deterministic default key from run/session/tool/action intent only
  when it is safe and well-defined; otherwise require the caller/tool planner to
  supply one before execution.
- Store execution records in a small local ledger keyed by user/session/tool/
  idempotency key.
- On duplicate committed execution, return the previous structured result or a
  safe duplicate-suppressed result instead of running the tool again.

Acceptance:

- Read-only tools still run without confirmation or idempotency overhead.
- Unknown or unclassified side-effecting tools default to conservative gate
  behavior.
- Duplicate side-effecting calls with the same idempotency key do not execute
  twice.
- Interrupt after committed side effect reports committed status rather than
  claiming cancellation.
- No real external provider is required for tests.

Candidate files:

- `src/assistant_agent/schemas/tools.py`
- `src/assistant_agent/tools/registry.py`
- `src/assistant_agent/agent/action_validator.py`
- `src/assistant_agent/agent/tool_executor.py`
- `src/assistant_agent/services/tool_history.py`
- `tests/unit/test_tool_registry.py`
- `tests/test_tool_executor.py`
- `tests/test_realtime_task_state.py`

## Phase 5: Lightweight Plan And Checkpoint For Recoverable Tasks

Goal: support recovery for long or multi-step realtime tasks without making
all requests go through a DAG planner.

Use plan/checkpoint only when:

- The task spans multiple tools with meaningful dependencies.
- The task may run long enough for interruption to be likely.
- A partially completed artifact can be safely reused.
- A side effect may require confirmation, compensation, or committed-status
  reporting.

Do not use plan/checkpoint for:

- Ordinary direct answers.
- Single read-only tool calls.
- Simple clarification turns.
- Short tool calls where native tool calling is enough.

Expected behavior:

- The runtime can save prompt-safe checkpoints after meaningful completed
  steps.
- Interrupt can choose `resume_from_checkpoint` only when a valid checkpoint
  exists.
- Stale checkpoints are not reused after the user explicitly restarts or
  invalidates previous work.

Acceptance:

- Tests cover restart, reuse-and-replan, resume-from-checkpoint, ask-confirm,
  compensate, and report-committed strategies.
- Checkpoint data is prompt-safe and bounded.
- The default non-realtime path does not become planner-dependent.

Candidate files:

- `src/assistant_agent/services/realtime_task_state.py`
- `src/assistant_agent/agent/runtime.py`
- `src/assistant_agent/agent/assistant_loop_nodes.py`
- `tests/test_realtime_task_state.py`
- `tests/test_native_tool_call_handoff.py`

## Validation Strategy

Use offline deterministic tests first:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_gateway.py \
  tests/test_gateway_session.py \
  tests/test_gateway_api.py \
  tests/test_realtime_agent_backend.py \
  tests/test_realtime_event_mapping.py \
  tests/test_realtime_backend_types.py \
  tests/test_realtime_task_state.py \
  tests/test_assistant_context_renderer.py \
  tests/test_tool_executor.py
```

For focused phases, run the smallest subset that covers the changed boundary.

Before claiming a phase complete:

- Run relevant tests.
- Run `git diff --check` on changed docs/source/tests.
- Confirm no real provider, real external media, secrets, or raw provider
  payloads were written.
- Update `docs/gateway-architecture.md`,
  `docs/tool-calling-architecture.md`, or
  `docs/CONTEXT_ENGINEERING_STATUS.md` if implementation changed their
  authority boundaries.

## Completion Tracking

| Phase | Status | Notes |
| --- | --- | --- |
| Phase 0: Baseline Lock And Metrics | Complete | Realtime progress metadata now reports first visible event timing, fallback emission, and visible event count. |
| Phase 1: Deterministic Realtime Fallback Messages | Complete | Realtime adapter emits display-only replaceable SLA fallback before delayed first visible output. |
| Phase 2: Realtime Call State Reducer Extensions | Complete | Prompt-safe call-state fields and reducer are wired into shared runtime events; tool completion, cancel, hangup, and TTS/display lifecycle events now update task state. |
| Phase 3: Explicit PreToolCall And PostToolCall Boundaries | Complete | Prompt-safe boundary summaries are attached at validator/executor boundaries without bypassing registry execution. |
| Phase 4: Risk Gate And Idempotency Ledger | Not started | Build on existing `ToolSpec.side_effect`. |
| Phase 5: Lightweight Plan And Checkpoint | Not started | Only for long or recoverable realtime tasks. |

## Next Recommended Step

Start Phase 4 with a narrow risk-gate and idempotency slice:

- Use existing `ToolSpec.side_effect` and the new PreToolCall/PostToolCall summaries as inputs.
- Add tests first for confirmation-sensitive side effects and duplicate idempotency keys.
- Keep read-only tools free of confirmation/idempotency overhead.
- Preserve Gateway wire frame names, assistant loop ownership, and memory-service boundaries unchanged.

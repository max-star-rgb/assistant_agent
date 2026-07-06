# Realtime Agent Task State Plan

Last updated: 2026-07-06

This is the medium-term development plan after the implemented realtime
cancel/interrupt phases. The current Gateway behavior already supports
`run.cancel`, same-session interrupt, queued ordinary messages, deadline
cancellation, stale-frame suppression, and App + Media Relay simulation. This
plan moves the next layer of work into the assistant runtime: preserving task
intent, classifying side effects, and deciding how a new interrupt should revise
the current task instead of always behaving like a blind restart.

`docs/gateway-architecture.md` remains the Gateway authority. This document is
an execution plan for task-state and realtime user-experience behavior.

## Purpose

Realtime interrupt should stay a control event at the Gateway boundary, but the
assistant runtime needs richer task semantics behind that boundary.

Target behavior:

- A user can interrupt an active realtime run and provide a revised constraint.
- Gateway still cancels the active run immediately and starts a new turn in the
  same session.
- The new assistant run receives enough task state to understand the original
  goal, the interrupt text, reusable non-side-effect artifacts, and any
  irreversible actions that already happened.
- The assistant chooses a safe continuation strategy: recompute, revise,
  resume, ask for confirmation, compensate, or report that an action already
  committed.
- App + Media presents realtime status that feels immediate: stop old output,
  show the interrupt text, show a short acknowledgement/progress state, and
  avoid displaying stale old-run content.

Non-goals for this medium phase:

- Do not change the public Gateway wire frame names.
- Do not add a second assistant loop or an OpenClaw-style runtime.
- Do not make Gateway responsible for planning, memory policy, tool choice, or
  semantic merge of task intent.
- Do not require true provider token streaming before task-state behavior can
  improve.
- Do not claim that interrupt can undo committed external side effects.

## Current Baseline

Already implemented:

- `/ws/realtime/media` maps `transcript.final` to `message.user`; explicit
  `interrupt=true` or `metadata.control=interrupt` cancels the active run and
  starts a new turn.
- Ordinary same-session messages queue behind the active run.
- Gateway passes session text history into `RealtimeAgentRequest.metadata`.
- Cancellation reaches the assistant graph and tool executor through a neutral
  cancel token.
- Gateway suppresses stale outbound frames from cancelled runs.
- App + Media has separate `Say`, `Interrupt`, `Cancel Agent`, and `Hang Up`
  controls.

Remaining gaps for medium-term realtime quality:

- Tool side effects now have an initial classification and task-state record,
  but a general pre-execution confirmation gate is not yet wired into
  `ActionValidator` / `ToolExecutor`.
- The App + Media UI now shows first-pass dedicated task-state progress
  statuses, but it does not yet provide interactive confirmation controls.
- Checkpoint/resume is modelled but not yet selected by persisted checkpoint
  records.

## Design Direction

Add task-state semantics behind the existing Gateway lifecycle.

Recommended layers:

1. Gateway lifecycle layer
   - Keep current session/run/cancel/interrupt behavior.
   - Continue emitting `run.started`, `event.progress`, `stream.chunk`, and
     `run.end`.
   - Treat task-state data as backend metadata, not Gateway protocol ownership.

2. Realtime adapter layer
   - Pass session history and interrupt metadata into the assistant runtime.
   - Surface task-state progress as display-only `run.progress` events.
   - Keep `RealtimeAgentRequest` wire-compatible; add optional metadata first
     before adding typed fields.

3. Assistant runtime layer
   - Build or load a session-scoped task-state snapshot before each run.
   - Record user intent, active constraints, intermediate observations, and
     side-effect status.
   - On interrupt, create an intent-revision event and select a continuation
     strategy.

4. Tool governance layer
   - Classify tools by side-effect behavior.
   - Require confirmation for irreversible external actions where appropriate.
   - Record whether a tool result is reusable, stale, pending, committed, or
     compensatable.

5. App + Media layer
   - Stop visually extending the old agent draft after interrupt.
   - Show the interrupt message as the new user turn immediately.
   - Display short statuses such as `Revising task`, `Using previous findings`,
     `Waiting for confirmation`, or `Action already sent`.

## Proposed Data Model

Use internal Pydantic models first. Do not expose these as public Gateway frames
until the behavior is proven.

Core records:

- `RealtimeTaskState`
  - `task_id`: stable id for the current user objective inside one session.
  - `session_id`, `user_id`, `created_at`, `updated_at`.
  - `objective`: current merged task objective.
  - `constraints`: active user constraints and preferences for the task.
  - `status`: `active`, `revising`, `waiting_for_user`, `completed`,
    `cancelled`, or `blocked`.
  - `source_turn_ids` and `source_run_ids`: provenance.

- `IntentRevision`
  - `revision_id`, `task_id`, `turn_id`, `run_id`.
  - `user_text`: interrupt or follow-up text.
  - `revision_type`: `add_constraint`, `replace_constraint`, `change_goal`,
    `cancel_goal`, `confirm`, or `clarify`.
  - `strategy`: `restart`, `reuse_and_replan`, `resume_from_checkpoint`,
    `ask_confirmation`, `compensate`, or `report_committed`.

- `TaskArtifact`
  - `artifact_id`, `task_id`, `run_id`.
  - `kind`: `observation`, `tool_result`, `media_ref`, `draft`,
    `decision`, or `checkpoint`.
  - `reuse_policy`: `reusable`, `stale`, `requires_validation`, or
    `do_not_reuse`.
  - `summary`: safe short description for prompt context.

- `SideEffectRecord`
  - `tool_name`, `run_id`, `task_id`.
  - `effect_level`: `none`, `local_read`, `external_read`,
    `pending_confirmation`, `committed`, or `compensatable`.
  - `confirmation_id` if human approval is required.
  - `compensation_hint` if a safe follow-up action exists.

Storage:

- Start with an in-memory task-state store for deterministic tests.
- Add an optional file or SQLite-backed store only after the model stabilizes.
- Keep long-term user memory separate; task state is session/task execution
  state, not user profile memory.

## Implementation Phases

### Phase A: Task-State Snapshot And Intent Revision

Goal: new interrupted runs can see a structured snapshot instead of only raw
text history.

Status: initial implementation landed on 2026-07-03.

Expected work:

- Add internal task-state models and an in-memory store. Done in
  `src/assistant_agent/services/realtime_task_state.py`.
- Load task state in `run_assistant_request(...)` or immediately before
  `AgentGraphRuntime.run_state(...)`.
- When realtime metadata indicates interrupt, create an `IntentRevision`.
- Include a concise task-state snapshot in runtime context/prompt input.
- Emit display-only progress when a run is revising an interrupted task.

Acceptance:

- Interrupt run sees original objective plus interrupt text in structured
  context.
- Non-realtime `/agent/run` remains unchanged unless task-state metadata is
  explicitly enabled.
- Unit tests cover first turn, queued follow-up, interrupt revision, and
  no-task-state fallback.

Current coverage:

- `tests/test_realtime_task_state.py` covers first turn, queued follow-up,
  interrupt revision, no-task-state fallback, and run-service injection.
- `tests/test_assistant_context_renderer.py` covers prompt/native/final-only
  task-state rendering.
- `tests/test_realtime_agent_backend.py` covers display-only revision progress.
- `tests/test_gateway_session.py` covers Gateway runtime metadata marking for
  interrupt versus ordinary queued messages.

### Phase B: Reusable Artifacts And Replan Strategy

Goal: interrupt does not blindly discard useful non-side-effect work.

Status: initial implementation landed on 2026-07-03.

Expected work:

- Record selected tool observations and media references as `TaskArtifact`.
  Done in `src/assistant_agent/services/realtime_task_state.py`; artifacts use
  prompt-safe observation compaction, not raw provider payloads.
- Mark artifacts stale or reusable when interrupt changes constraints.
- Add a simple strategy selector:
  - pure generation: `restart`
  - search/compare/analysis: `reuse_and_replan`
  - long report/build task: `resume_from_checkpoint`
  - side-effect task: `ask_confirmation` or `report_committed`
- Pass reusable artifact summaries into the next run context.

Acceptance:

- Tests show an interrupted compare/search task can reuse previous observations
  while changing ranking criteria.
- Stale artifacts are not reintroduced into final answer context.
- Strategy decisions are visible in trace/progress without exposing chain of
  thought.

Current coverage:

- `tests/test_realtime_task_state.py` covers reusable product-search artifacts,
  stale artifact invalidation, prompt exclusion for stale artifacts, and
  strategy progress payloads.
- `tests/test_realtime_event_mapping.py` covers `task_state/revising` progress
  payload preservation through realtime event mapping.

Current limitation:

- Checkpoint resume remains modelled but not selected until durable checkpoint
  records are introduced. Side-effect strategies are selected by the Phase C
  first-pass `SideEffectRecord` implementation.

### Phase C: Side-Effect Classification And Human Confirmation

Goal: interrupt behavior is safe around actions that cannot be simply cancelled.

Status: initial classification/recording implementation landed on 2026-07-03.

Expected work:

- Add side-effect metadata to tool specs or tool policy records.
- Default unknown tools to conservative behavior.
- Record pending confirmation state from tool results that expose
  `requires_confirmation` or `confirmation_id`.
- If interrupt arrives after a committed action, new run must report committed
  state and offer a safe follow-up or compensation path.
- Future remaining work: before irreversible external actions, require
  confirmation through a shared service rather than only recording tool result
  state after execution.

Acceptance:

- Tests cover read-only tool, pending confirmation, committed action,
  compensatable action, and conservative unknown-tool behavior.
- Interrupt never claims a committed side effect was cancelled.
- Existing validator/executor boundaries remain the only path to tools.

Current coverage:

- `ToolSpec.side_effect` now carries static side-effect policy into prompt-json,
  provider-native, MCP, and registry descriptions.
- Unknown tools default to `pending_confirmation`; if such a tool already
  succeeded, realtime task-state records it as `committed`.
- Realtime task-state records prompt-safe `SideEffectRecord`s for completed
  runs and selects `ask_confirmation`, `report_committed`, or `compensate`
  before ordinary artifact reuse/restart strategies.
- Progress payloads expose side-effect counts without changing Gateway frame
  names.

Current limitation:

- No general human-confirmation service is yet wired in front of arbitrary
  irreversible tools. `memory_save` still uses its existing memory-specific
  confirmation flow; other tools only contribute side-effect records after
  they return.

### Phase D: App + Media Realtime UX

Goal: the UI communicates realtime intent revision clearly.

Status: first-pass task-state status mapping landed on 2026-07-06.

Expected work:

- Add App + Media statuses for `Revising task`, `Using previous findings`,
  `Waiting for confirmation`, and `Action committed`.
- On Interrupt, close or mark the current agent draft as cancelled/stale before
  showing the new user message.
- Keep raw Gateway frames inside the Gateway Timeline only.
- Preserve Web Chat layout and mode selector behavior.

Current coverage:

- App + Media consumes `event.progress` payloads with `stage=task_state`.
- `reuse_and_replan` maps to `Using previous findings`; `ask_confirmation`
  maps to `Waiting for confirmation`; `report_committed` maps to
  `Action committed`; `compensate` maps to `Preparing follow-up`; fallback
  revision maps to `Revising task`.
- Interrupt marks the active pending agent draft as stale/cancelled before
  appending the new user turn.
- Gateway raw frames remain in the collapsed Gateway Timeline.
- Static console tests assert Web Chat and App + Media remain separate modes
  and that the new task-state status strings are present.

Current limitation:

- `Waiting for confirmation` is display-only. There is still no generic
  confirmation accept/reject control for arbitrary non-memory tools.

Acceptance:

- Manual flow: start a compare task, interrupt with a new constraint, see the
  old output stop and the revised task continue in the same call transcript.
- Manual flow: interrupt a side-effect task after confirmation/commit and see a
  truthful committed-state message.
- Static page tests still assert Web Chat and App + Media are separate modes.

## Testing Plan

Core offline tests:

- Task-state model validation and store behavior.
- Runtime context receives task-state snapshot only when enabled.
- Interrupt creates an `IntentRevision` and selects the expected strategy.
- Reusable artifact summaries are included; stale artifacts are excluded.
- Side-effect policy prevents unsafe "cancelled" claims after commit.
- Gateway interrupt/queue/cancel tests remain unchanged.
- App + Media static tests cover new statuses and draft-stale behavior.

Recommended validation for implementation phases:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_gateway_session.py \
  tests/test_gateway_api.py \
  tests/test_realtime_agent_backend.py \
  tests/test_phase6b_web_console.py \
  tests/test_phase7c_web_productization.py
```

When code touches runtime context, tools, memory, or task-state stores, also run
the relevant assistant runtime, tool executor, and context tests.

## Open Decisions

Default choices for implementation unless a later plan changes them:

- Task state starts as opt-in for Gateway/realtime runs and does not change
  ordinary `/agent/run` behavior.
- The first store is in-memory and test-friendly.
- Task-state snapshots are summarized before entering prompts; raw tool
  payloads are not injected wholesale.
- Unknown side-effect level is treated conservatively.
- Public Gateway frame names remain stable. New task-state details should first
  travel through backend metadata and display-only progress events.

Questions to resolve before Phase C implementation:

- Whether non-memory irreversible tools should use a new generic confirmation
  service or reuse a narrower tool-specific confirmation contract.
- Whether durable task-state persistence is required before a real pilot, or
  whether in-memory state is enough for local App + Media validation.

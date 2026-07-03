# Realtime Agent Task State Plan

Last updated: 2026-07-03

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

Missing for medium-term realtime quality:

- There is no structured task-state object that spans interrupted runs.
- Reusable intermediate results are not separated from final answer text.
- Tool side effects are not classified as safe, pending confirmation, or
  committed from the interrupt/resume perspective.
- Interrupt does not yet produce a first-class "intent revision" record.
- The App + Media UI does not yet show a dedicated acknowledgement/revision
  state separate from generic cancelling/thinking states.

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

Expected work:

- Add internal task-state models and an in-memory store.
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

### Phase B: Reusable Artifacts And Replan Strategy

Goal: interrupt does not blindly discard useful non-side-effect work.

Expected work:

- Record selected tool observations and media references as `TaskArtifact`.
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

### Phase C: Side-Effect Classification And Human Confirmation

Goal: interrupt behavior is safe around actions that cannot be simply cancelled.

Expected work:

- Add side-effect metadata to tool specs or tool policy records.
- Default unknown tools to conservative behavior.
- Before irreversible external actions, require confirmation or record a
  pending confirmation state.
- If interrupt arrives after a committed action, new run must report committed
  state and offer a safe follow-up or compensation path.

Acceptance:

- Tests cover read-only tool, pending confirmation, committed action, and
  compensatable action.
- Interrupt never claims a committed side effect was cancelled.
- Existing validator/executor boundaries remain the only path to tools.

### Phase D: App + Media Realtime UX

Goal: the UI communicates realtime intent revision clearly.

Expected work:

- Add App + Media statuses for `Revising task`, `Using previous findings`,
  `Waiting for confirmation`, and `Action committed`.
- On Interrupt, close or mark the current agent draft as cancelled/stale before
  showing the new user message.
- Keep raw Gateway frames inside the Gateway Timeline only.
- Preserve Web Chat layout and mode selector behavior.

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

- Which existing tools should be explicitly classified as side-effect-free,
  confirmation-required, or committed-action tools?
- Whether confirmation state should reuse existing memory confirmation
  structures or live in a separate task-state store.
- Whether durable task-state persistence is required before a real pilot, or
  whether in-memory state is enough for local App + Media validation.

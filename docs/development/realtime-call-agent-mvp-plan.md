# Realtime Call Agent MVP Plan

Last updated: 2026-07-07

This is a short execution plan for the first practical realtime-call-agent
slice. It is not an architecture authority. The architecture authorities remain:

- `docs/gateway-architecture.md` for Gateway, realtime frames, session/run,
  cancel, interrupt, reconnect, hangup, and stream lifecycle boundaries.
- `docs/tool-calling-architecture.md` for `ToolSpec`, `ActionValidator`,
  `ToolExecutor`, `ToolRegistry`, provider-native tool calls, tool observations,
  retry, recovery, side-effect policy, and tool governance.
- `docs/development/realtime-harness-hardening-plan.md` for the broader
  medium-term realtime harness roadmap.

## Goal

Build confidence in a text-level realtime call loop before adding real phone
SDK, STT, TTS, or a larger skill system.

The first MVP target is:

```text
transcript.final
  -> Gateway session/run lifecycle
  -> AgentGraphRealtimeBackend
  -> AgentGraphRuntime / assistant loop
  -> event.progress / stream.chunk / run.end
```

The MVP must support cancel, interrupt, per-session text history, deterministic
progress/fallback behavior, and one read-only tool smoke path. This proves the
realtime lifecycle and the current LangGraph runtime can work together before
more business skills are added.

## Runtime Decision

Continue using `AgentGraphRuntime` as the assistant runtime.

The reason is not that an Anthropic-style runtime is bad. It is that it solves a
different packaging problem: a Claude-first loop can bundle provider calls,
tool use, skill loading, file/browser/http operations, and turn-ending policy in
one runtime. This project needs a provider-neutral, locally testable runtime
where Gateway lifecycle, provider selection, memory, trace, budget, audit, and
tool side-effect policy stay separated.

Therefore:

- `AgentGraphRuntime` remains the only internal assistant execution loop.
- `assistant_agent.realtime.AgentGraphRealtimeBackend` remains a thin adapter
  from Gateway requests/events/results into the shared assistant run service.
- Provider-native tool calls are allowed only as an adapter input path; after
  normalization they still go through `ActionValidator -> ToolExecutor ->
  ToolRegistry`.
- The legacy `/home/lenovo1/pycharm_project/runTime` Anthropic/OpenClaw agent
  loop is reference material only. Do not import it, wrap it around
  `assistant_agent`, or add adapter selection that can replace
  `AgentGraphRuntime`.

## What To Borrow From runTime

Borrow Gateway protocol and lifecycle behavior:

- Stable frames such as `message.user`, `run.started`, `event.progress`,
  `stream.chunk`, `run.end`, `run.cancel`, `ping`, `pong`, `call.incoming`,
  `call.hangup`, and `config.update`.
- Session/run ids, turn ids, active-run registration, per-session text history,
  `expects_reply`, cancel, same-session interrupt, hangup cleanup, deadline
  cancellation, and stale event suppression.
- Compatibility tests or behavior checks where they clarify wire semantics.

Borrow the skill idea, not the old skill runtime:

- A skill is a capability/workflow description with a small `SKILL.md` style
  contract, progressive disclosure, optional references/assets, and optional
  deterministic steps.
- A skill is not a privileged execution path. Runtime skill execution must not
  call external services, browser automation, shell commands, or registered
  tools directly.
- Any executable step must become an existing governed tool or a new governed
  tool with `ToolSpec`, validation, executor behavior, mock/local defaults,
  trace, tests, and docs.
- The first skill-style layer should be small: use it to explain when to choose
  an existing read-only capability, then let the assistant loop invoke the
  normal tool path.

Do not borrow:

- The old Anthropic/OpenClaw agent loop.
- Direct `run_skill` execution that bypasses current tool governance.
- Automatic real-provider activation based on detected API keys.
- Raw browser, shell, HTTP, or external-service execution from a skill engine.

## Current Baseline

The current codebase already has these foundations:

- Gateway owns normalized realtime session/run/cancel/interrupt/reconnect/
  hangup and stream-frame semantics.
- `/ws/realtime/media` maps media-entry events into Gateway frames.
- `AgentGraphRealtimeBackend` adapts `RealtimeAgentRequest` into `UserRequest`
  and forwards events from the shared assistant run service.
- `RealtimeCancelToken` reaches `AgentGraphRuntime` and `ToolExecutor`.
- Gateway suppresses stale backend events after cancel, interrupt, or deadline
  cancellation.
- Realtime progress mapping supports runtime lifecycle, tool lifecycle,
  response chunks, final responses, errors, and task-state progress.
- Deterministic first-progress fallback and heartbeat policy exist in the
  realtime adapter path.
- Tool calls remain behind `ActionValidator`, `ToolExecutor`, and
  `ToolRegistry`.
- A minimal prompt-safe capability catalog can expose `realtime_web_search` as a
  skill-style descriptor backed by the governed `web_search` tool. It is
  selection context only and does not add a new execution path.

This means the MVP work should start by locking and validating the baseline,
not by replacing the runtime.

## MVP Scope

In scope:

- Text-level realtime call loop through Gateway and `AgentGraphRealtimeBackend`.
- `session.start`, `transcript.final`, `run.cancel`, `session.end`, `ping`, and
  interrupt behavior through `/ws/realtime/media`.
- `message.user`, `run.started`, `event.progress`, `stream.chunk`, and
  `run.end` Gateway behavior.
- Per-session text history passed to the assistant backend.
- Deterministic first-progress fallback before slow model/tool output.
- Heartbeat progress for long-running turns.
- Stale frame suppression after cancel, interrupt, hangup, or deadline.
- One read-only governed tool smoke path, preferably an existing mock/local
  search or product lookup path.
- A minimal skill-style workflow description that routes to existing governed
  tool capabilities instead of introducing a second execution engine.

Out of scope:

- Real telephony SDK integration.
- Real STT or TTS services.
- Raw audio streaming through Gateway.
- Token-level model streaming as a requirement.
- A full skill engine.
- A second assistant loop or Anthropic/OpenClaw runtime replacement.
- Side-effecting external actions such as orders, notifications, payments, or
  database writes.
- New dependencies or network dependency installation.

## Recommended First Stage

Choose lifecycle-first over tool-rich-first.

The first stage should prove that a caller can speak, interrupt, cancel, hang
up, and receive coherent realtime frames. Tool richness can wait because tools
become much harder to debug if the session lifecycle is unstable.

The first stage is complete when:

- A text transcript turn creates one active run and reaches `run.end`.
- A slow run emits one replaceable progress/fallback event before final output.
- A same-session interrupt cancels or gates old output and starts a new turn.
- Ordinary same-session follow-up without interrupt queues behind the active
  run.
- `run.cancel` and `session.end` suppress stale output and emit terminal
  lifecycle frames.
- Per-session history is visible to the backend and bounded by existing
  context rules.
- A read-only tool path emits progress/tool lifecycle events and final answer
  chunks without bypassing `ToolExecutor`.

## Implementation Slices

### Slice 0: Baseline Evidence

Purpose: establish what is already working before changing code.

Actions:

- Run the targeted realtime/Gateway tests listed below.
- Record failures as specific implementation tasks instead of adding new
  architecture.
- Do not touch provider, memory, or tool internals unless a failing test proves
  the MVP path needs it.

Validation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_realtime_agent_backend.py \
  tests/test_realtime_event_mapping.py \
  tests/test_realtime_backend_types.py \
  tests/test_gateway.py \
  tests/test_gateway_session.py \
  tests/test_gateway_api.py
```

### Slice 1: Text Realtime Lifecycle Lock

Purpose: lock transcript-to-response behavior through Gateway.

Candidate files:

- `src/assistant_agent/api/gateway_websocket.py`
- `src/assistant_agent/gateway/session.py`
- `src/assistant_agent/gateway/event_mapping.py`
- `src/assistant_agent/realtime/agent_graph_backend.py`
- `tests/test_gateway_session.py`
- `tests/test_gateway_api.py`
- `tests/test_realtime_agent_backend.py`

Acceptance:

- `/ws/realtime/media` accepts `session.start`, `transcript.final`,
  `run.cancel`, `session.end`, and `ping`.
- Invalid media events return safe `error` frames and do not enter the backend.
- `transcript.final` maps to `message.user` with text and media references.
- Trusted session config, not user payload metadata, controls realtime phone
  profile selection.
- Gateway history is passed in backend metadata.
- Existing non-Gateway `/agent/run` and product WebSocket behavior stay
  unchanged.

### Slice 2: Progress, Fallback, And Stale Output

Purpose: make realtime waiting behavior deterministic and safe.

Candidate files:

- `src/assistant_agent/realtime/progress.py`
- `src/assistant_agent/realtime/event_mapping.py`
- `src/assistant_agent/realtime/agent_graph_backend.py`
- `src/assistant_agent/gateway/session.py`
- `tests/test_realtime_agent_backend.py`
- `tests/test_realtime_event_mapping.py`
- `tests/test_gateway_session.py`

Acceptance:

- A slow run emits one display-only fallback with
  `source="realtime_sla_fallback"` and `replaceable=true`.
- A run that emits quick progress does not emit the fallback.
- A cancelled or hung-up run does not emit fallback after cancellation.
- Heartbeat progress is display-only and prompt-safe.
- Final response chunks are still delivered for completed runs.
- Stale chunks, final events, tool events, and errors from cancelled runs are
  suppressed at the Gateway boundary.

### Slice 3: Read-Only Tool Smoke

Purpose: prove realtime + tools works without adding side-effect risk.

Candidate files:

- `src/assistant_agent/agent/runtime.py`
- `src/assistant_agent/realtime/event_mapping.py`
- `src/assistant_agent/realtime/agent_graph_backend.py`
- `src/assistant_agent/tools/registry.py`
- `tests/test_realtime_agent_backend.py`
- `tests/test_realtime_event_mapping.py`
- relevant existing tool tests for the chosen read-only tool

Acceptance:

- Use an existing read-only mock/local tool path.
- The model or scripted adapter chooses a provider-native tool call.
- The native tool call normalizes to `AssistantDecision(type="tool_call")`.
- `ActionValidator` accepts the input.
- `ToolExecutor` emits started/finished or failed lifecycle events.
- Gateway maps user-visible progress and final chunks.
- No test or implementation directly calls `ToolRegistry.run(...)` from a new
  entrypoint.

### Slice 4: Minimal Skill-Style Capability Layer

Purpose: borrow the useful skill idea without adding a second runtime.

Status: implemented for the first read-only capability descriptor.

Initial design:

- Start with one or two repo-local capability descriptions, not a broad dynamic
  marketplace.
- Each description has `name`, `description`, `when_to_use`,
  `when_not_to_use`, required inputs, safe examples, and the governed tool or
  tool sequence it maps to.
- If deterministic steps are needed, each step names an existing governed tool.
  Steps must not contain shell commands, raw HTTP calls, browser automation, or
  provider-specific SDK calls.
- The assistant receives a concise capability catalog or selected capability
  context. It does not receive unbounded skill bodies by default.
- Execution remains LLM-first for selection and `ToolExecutor`-first for
  effects.

Implemented files:

- `docs/development/realtime-call-agent-mvp-plan.md`
- `docs/CONTEXT_ENGINEERING_STATUS.md`
- `src/assistant_agent/schemas/context.py`
- `src/assistant_agent/services/context/capability_catalog.py`
- `src/assistant_agent/services/context/builder.py`
- `src/assistant_agent/services/context/renderer.py`
- `tests/test_tool_catalog.py`
- `tests/test_assistant_context_renderer.py`

Acceptance:

- The skill-style layer improves capability selection context without adding a
  new execution path.
- Existing `ToolSpec` remains the authoritative executable contract.
- Capability descriptors are selected only when their governed tools are
  available and already present in the prompt tool catalog.
- Provider-native context can include the selected capability catalog without
  rendering the full ToolSpec list.
- Any new executable capability has tool spec, validator coverage, executor
  coverage, mock/local provider behavior, and docs.

## Manual Smoke Flow

Run the app with mock/local defaults:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py --provider mock --image-provider mock
```

Exercise `/ws/realtime/media` with:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/realtime_media_client.py
```

Expected behavior:

- `session.start` produces call-ready lifecycle output.
- A normal transcript produces `run.started`, progress if delayed,
  `stream.chunk`, and `run.end`.
- `run.cancel` ends the active run with cancelled semantics.
- An interrupt transcript starts a new turn and suppresses old-run output.
- `session.end` cancels active work and acknowledges hangup.

## Completion Criteria

The MVP is ready for real STT/TTS exploration only after:

- Targeted realtime/Gateway tests pass in mock/local mode.
- A manual text-level media smoke can run end to end.
- The runtime decision is documented and not under active dispute.
- `runTime` reference use is limited to Gateway lifecycle and skill ideas.
- One read-only governed tool path works through realtime progress and final
  response frames.
- No second assistant loop, direct skill executor, or real provider auto-enable
  path has been introduced.

After this MVP, the next practical phase is real audio edge integration:
connect STT/TTS or a phone SDK as an entry adapter that emits the same Gateway
frames. That phase should not change the assistant runtime boundary.

## Post-MVP Stage 2: Audio Edge Contract

Status: started with the smallest safe contract.

Implemented in this step:

- `transcript.final` can carry sanitized `media_edge` metadata for transcript,
  STT, TTS, and `audio_id` references.
- `payload.metadata`, `stt`, `tts`, and session `config.stt` / `config.tts`
  remove raw audio, base64, provider raw responses, API keys, and secret-like
  fields before backend request construction.
- `gateway_frame_to_tts_event()` maps speakable Gateway text frames
  (`stream.chunk` and `event.progress`) into TTS edge events without invoking a
  provider.

Stop point for this development stage:

- Stop after sanitized STT input metadata and TTS output event mapping are
  tested in mock/local mode.
- Do not connect a real STT/TTS SDK, stream raw audio, add provider credentials,
  or change `AgentGraphRuntime` in this stage.

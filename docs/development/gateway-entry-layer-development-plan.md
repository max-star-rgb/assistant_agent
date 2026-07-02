# Gateway Entry Layer Development Plan

Last updated: 2026-07-02

This document is the current execution plan for making Gateway responsibilities
explicit in implementation. It is not the architecture authority. The
architecture authority remains `docs/gateway-architecture.md`.

When this plan is completed, keep it as a historical development record unless
there is a new active Gateway implementation phase.

Status: implemented for the local/offline Gateway frame route, realtime media
entry adapter, and smoke client on 2026-07-02. Keep this file as the development
record for the phase.

## Objective

Make entry layers and Gateway responsibilities explicit in code, tests, and
operator-facing behavior:

- Entry layers own product and transport concerns: CLI, Web UI, app, HTTP,
  WebSocket, and realtime call adapters.
- Gateway owns normalized message/session/run lifecycle semantics for
  Gateway-managed traffic.
- `assistant_agent.realtime` remains the boundary from Gateway to the assistant
  runtime.
- `AgentGraphRuntime` remains the only assistant execution loop.

The first implementation target is an explicit Gateway entry adapter path that
can be used by realtime call clients and protocol-level tests without replacing
the existing `/agent/run`, CLI, eval, or Web demo request/response paths.

## Current Baseline

Implemented:

- `src/assistant_agent/gateway/protocol.py` defines Gateway frame helpers and
  protocol constants.
- `src/assistant_agent/gateway/bridge.py` handles external-client-to-session
  frame forwarding, call lifecycle, stale bridge eviction, disconnect cancel,
  and Gateway-level modality checks.
- `src/assistant_agent/gateway/session.py` handles `message.user`,
  `run.cancel`, session history, active run lifecycle, interrupt, deadlines,
  realtime event mapping, and per-user session management.
- `src/assistant_agent/gateway/ws.py` adapts JSON text WebSocket messages to a
  Gateway endpoint.
- `src/assistant_agent/gateway/ws_server.py` provides an optional standalone
  session-side WebSocket server.
- `src/assistant_agent/realtime/agent_graph_backend.py` adapts Gateway realtime
  requests to `run_assistant_request`.
- Existing `/agent/run`, `/agents/run`, `/ws/agent/{session_id}`, CLI, eval, and
  Web demo paths continue to run through existing API or shared run-service
  entrypoints.

Resolved gaps in this phase:

- The main FastAPI app now exposes `/ws/gateway` as an explicit Gateway frame
  WebSocket entry adapter.
- Current WebSocket product route `/ws/agent/{session_id}` remains unchanged as
  a direct product entrypoint, not a Gateway protocol route.
- `scripts/run_gateway_client.py` provides a local operator smoke command for
  the Gateway frame protocol.
- `tests/test_gateway_api.py` covers Gateway entry-layer auth, identity,
  protocol flow, cancel, unsupported modality, and media-service mapping.
- `docs/gateway-architecture.md` documents when clients should use Gateway
  lifecycle semantics rather than simple request/response routes.

## Non-goals

- Do not replace `/agent/run`, `/agents/run`, CLI, eval, or Web demo behavior.
- Do not route all simple request/response traffic through Gateway.
- Do not import `openclaw_gateway_runtime` or reuse legacy `runTime` agent loop
  code.
- Do not add a second assistant loop, provider loop, or OpenClaw Anthropic
  adapter path.
- Do not introduce real provider calls, external telephony services, TTS/STT
  services, mobile app code, or remote dependency installation.
- Do not change tool-calling, memory-service, provider, or AgentRouter
  governance boundaries.
- Do not promise token-level model streaming or hard provider cancellation in
  this phase.

## Phase 0: Baseline And Invariants

Goal: lock the current intended boundary before implementation changes.

Tasks:

- Keep `docs/gateway-architecture.md` as the Gateway authority.
- Keep this plan scoped to implementation work and acceptance criteria.
- Confirm current Gateway tests cover `message.user`, `run.cancel`, same-session
  interrupt, deadline cancellation, history, `expects_reply`, stale-event
  suppression, and WebSocket frame encoding/decoding.
- Identify existing product WebSocket behavior that must remain unchanged.

Acceptance:

- `tests/test_gateway.py` passes.
- `tests/test_gateway_session.py` passes.
- `tests/test_realtime_agent_backend.py`,
  `tests/test_realtime_event_mapping.py`, and
  `tests/test_realtime_backend_types.py` pass.
- No product route is migrated or renamed in this phase.

## Phase 1: Gateway Service Ownership In FastAPI

Goal: create a clear application-owned Gateway manager/bridge boundary without
changing existing routes.

Expected implementation:

- Implemented: add a small API service/factory module for process-local
  `GatewaySessionManager` and `GatewayBridge` instances.
- Implemented: use `AgentGraphRealtimeBackend` as the default backend factory.
- Implemented: keep manager settings local/offline safe by default:
  `max_sessions`, `idle_timeout_s`, `hangup_grace_s`, and reaper behavior should
  be configurable without requiring real providers.
- Implemented: add lifespan cleanup hooks if a long-lived reaper task is started
  by the app.

Candidate files:

- `src/assistant_agent/api/gateway_runtime.py`
- `src/assistant_agent/gateway/session.py`
- `tests/test_gateway_api.py`

Acceptance:

- Existing HTTP and WebSocket routes behave the same.
- Gateway manager reuse is deterministic in tests.
- Shutdown or explicit test cleanup does not leave running tasks.
- No provider is called unless an existing opt-in runtime profile allows it.

## Phase 2: Explicit Gateway WebSocket Entry Adapter

Goal: expose a product-neutral Gateway frame WebSocket route in the main FastAPI
app.

Expected implementation:

- Implemented: add `/ws/gateway`, which accepts Gateway JSON text frames.
- Implemented: wrap the accepted FastAPI WebSocket with an Endpoint-compatible
  adapter.
- Implemented: bridge external frames through `GatewayBridge` and
  `GatewaySessionManager`.
- Implemented: support at least `ping`, `call.incoming`, `message.user`, `run.cancel`,
  `call.hangup`, and `config.update`.
- Implemented: reuse existing API auth and identity policy before dispatch.
- Implemented: return stable Gateway protocol error frames for malformed or unauthorized
  input where possible.
- Implemented: keep `/ws/agent/{session_id}` unchanged as the current product WebSocket
  request/response entrypoint.

Candidate files:

- `src/assistant_agent/api/gateway_websocket.py`
- `src/assistant_agent/api/app.py`
- `tests/test_gateway_api.py`
- `tests/test_gateway_auth.py`

Acceptance:

- A Gateway client can send `call.incoming`, receive `call.ready`, send
  `message.user`, and receive `run.started`, `stream.chunk`, and `run.end`.
- `run.cancel` cancels the active Gateway run and suppresses stale chunks.
- New `message.user` for the same session interrupts the previous active run.
- `ping` returns `pong`.
- Unsupported modality returns `unsupported_modality`.
- Existing `/ws/agent/{session_id}` tests still pass.

## Phase 3: Gateway Client And Smoke Coverage

Goal: give local operators and tests a simple way to exercise the Gateway frame
protocol.

Expected implementation:

- Implemented: add a small script that opens the Gateway WebSocket route and
  sends Gateway frames.
- Implemented: keep it local/offline by default and compatible with mock profile.
- Implemented: do not replace `scripts/run_client.py`; this is a separate protocol smoke
  path.

Candidate files:

- `scripts/run_gateway_client.py`
- `tests/test_gateway_api.py`
- `tests/test_run_server.py`

Acceptance:

- The smoke path can run against local server with mock/default profile.
- Output includes the frame sequence, final `run.end` reason, and safe error
  details when the run fails.
- The script does not log tokens, raw provider payloads, or large media bodies.

## Phase 4: Entry Adapter Migration Guidance

Goal: document when entry adapters should use Gateway and when they should call
the shared run service directly.

Expected implementation:

- Implemented: update `docs/gateway-architecture.md` for the implemented route and
  current code map.
- Implemented: add brief operator or developer guidance for:
  - simple request/response route: use `run_assistant_request`.
  - realtime session/run lifecycle route: use Gateway.
  - product WebSocket route: keep current direct path unless it needs Gateway
    lifecycle semantics.
  - realtime call adapter: map transport events into Gateway frames.
- Keep `README.md` as a placeholder unless the user explicitly asks to rewrite
  it.

Candidate files:

- `docs/gateway-architecture.md`
- `docs/development/gateway-entry-layer-development-plan.md`
- `.codex/skills/assistant-runtime-reference/SKILL.md`

Acceptance:

- A new contributor can identify which layer owns CLI, Web UI, app, HTTP,
  WebSocket, realtime call transport, Gateway, realtime backend, and assistant
  runtime behavior.
- The docs do not describe Gateway as a product entrypoint.
- The docs do not describe `runTime` as current project architecture.

## Phase 5: Realtime Call Adapter Readiness

Goal: prepare for realtime call integration without taking ownership of
external telephony, STT, or TTS services inside the assistant runtime.

Expected implementation:

- Implemented: define the expected adapter mapping from media-service events to Gateway frames:
  `call.incoming`, `message.user`, `run.cancel`, `call.hangup`, and
  `config.update`.
- Implemented: keep audio/STT/TTS provider details outside core Gateway unless explicitly
  requested.
- Implemented: ensure Gateway can carry multimodal references safely even if only text is
  processed in the current phase.

Acceptance:

- The realtime call adapter design can be implemented as an entry layer over
  Gateway.
- Gateway remains independent of concrete telephony SDKs.
- Unsupported modalities continue to return explicit Gateway errors until
  support is intentionally added.

## Validation Commands

For doc-only changes:

```bash
git diff --check -- docs/gateway-architecture.md docs/development/gateway-entry-layer-development-plan.md .codex/skills/assistant-runtime-reference/SKILL.md AGENTS.md
```

For Gateway behavior changes:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_session.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py
```

For API entry adapter changes, add the smallest relevant API/WebSocket tests,
then run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py tests/test_gateway_session.py tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py
```

Add any new API tests to the command once the route names and files exist.

Current API entry adapter tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py tests/test_websocket_graph_runtime.py
```

## Implementation Risks

- Mixing two WebSocket protocols can confuse clients. Keep `/ws/agent/{session_id}`
  and the Gateway frame route clearly separate.
- Gateway session history and shared conversation history are different scopes.
  Avoid duplicating or leaking history across those boundaries.
- Auth-bound identity must be enforced before Gateway dispatch when production
  identity mode is enabled.
- Late events from cancelled runs must stay suppressed at the Gateway boundary.
- App-level Gateway manager tasks must not leak across tests.
- Do not let Gateway become a shortcut around tool governance; all assistant
  execution must still flow through the existing runtime and tool executor.

## Completion Criteria

This plan is complete when:

- Implemented: the main FastAPI app exposes `/ws/gateway` as an explicit Gateway
  frame WebSocket entry adapter.
- Implemented: existing product HTTP/WebSocket/CLI behavior remains backward
  compatible.
- Implemented: Gateway route tests cover call setup, message run, cancel,
  unsupported modality, auth/identity failure, and media-service text/video
  mapping. Same-session interrupt remains covered in `tests/test_gateway_session.py`.
- Implemented: local Gateway smoke tooling exists in `scripts/run_gateway_client.py`.
- Implemented: `docs/gateway-architecture.md` matches the implemented route and
  code map.
- Implemented: this plan is marked implemented as a historical development
  record for the phase.

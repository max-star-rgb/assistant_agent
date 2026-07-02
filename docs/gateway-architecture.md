# Gateway Architecture

Last updated: 2026-07-02

This document is the current canonical entry for `assistant_agent.gateway`, realtime Gateway protocol frames, entry-layer boundaries, and the Gateway-to-assistant runtime contract. Update it whenever Gateway responsibilities, realtime call behavior, Gateway WebSocket bridging, session/run/cancel semantics, or entry adapter routing changes.

## Quick Handoff

- Gateway is not a product entrypoint. CLI, Web UI, app, HTTP, WebSocket, and realtime call adapters are entry layers.
- Gateway owns normalized message, session, run, cancel, interrupt, reconnect, hangup, and stream-frame semantics between entry layers and the assistant realtime backend.
- `assistant_agent.realtime` is the contract between Gateway and the current assistant runtime. The default backend remains `AgentGraphRealtimeBackend`.
- `AgentGraphRuntime` and the assistant loop remain the internal agent executor. Do not add an OpenClaw-style second agent loop.
- Existing `/agent/run`, CLI, eval, and Web demo paths may continue to call the shared assistant run service directly when they do not need Gateway session/run lifecycle semantics.
- The main FastAPI app exposes `/ws/gateway` for Gateway JSON frames and `/ws/realtime/media` for media-service events that are adapted into Gateway frames.
- OpenClaw / `runTime` is compatibility reference material for wire protocol and lifecycle behavior only. Do not import it into this project.

## Layering

Product and transport adapters live at the entry layer:

```text
CLI / Web UI / app / HTTP route / WebSocket route / realtime call transport
        |
        v
entry adapter: auth, transport IO, product payload parsing, user experience contract
```

Gateway is the normalized realtime run boundary behind those entry adapters:

```text
entry adapter
        |
        v
GatewayBridge / GatewaySessionManager / GatewaySessionService
        |
        v
RealtimeAgentRequest / RealtimeAgentEvent / RealtimeAgentResult
        |
        v
AgentGraphRealtimeBackend
        |
        v
AgentGraphRuntime / assistant loop
        |
        v
ActionValidator -> ToolExecutor -> ToolRegistry -> tools / providers / memory
```

The default non-realtime product path can stay simpler:

```text
CLI / HTTP / Web UI
        |
        v
run_assistant_request
        |
        v
AgentGraphRuntime / assistant loop
```

That path is valid when the caller only needs one request/response run and does not need Gateway-managed reconnect, hangup, active-run cancellation, interrupt, stream-frame compatibility, or per-user realtime session reuse.

## Gateway Responsibilities

Gateway owns the protocol and lifecycle boundary for realtime or Gateway-normalized traffic:

- Accept normalized frames such as `message.user`, `run.cancel`, `ping`, `call.incoming`, `call.hangup`, and `config.update`.
- Validate Gateway-level modality support before dispatching to the assistant backend.
- Bind or preserve `user_id`, `session_id`, `turn_id`, and `run_id`.
- Maintain per-session user text history for Gateway turns.
- Register active runs and emit `run.started`, `stream.chunk`, and `run.end`.
- Convert realtime backend events into Gateway wire frames.
- Convert backend failures into protocol-level `run.end` or `error` frames.
- Cancel active runs on explicit `run.cancel`, disconnect, deadline expiry, or same-session interrupt.
- Manage per-user session reuse, reconnect, hangup grace, idle eviction, and live session config.
- Keep external connection lifecycle separate from the assistant runtime internals.

Gateway should remain transport-agnostic where possible. WebSocket handling belongs in an adapter such as `gateway.ws` or an API entry route, while Gateway session behavior belongs in `gateway.session`.

## Entry Layer Responsibilities

Entry adapters own product and transport concerns before a request reaches Gateway or the shared assistant run service:

- CLI argument parsing and local command UX.
- Web UI request shape, browser event handling, and display-specific streaming behavior.
- Mobile/app request shape and platform-specific connection lifecycle.
- HTTP route parsing, response schema, request validation, and FastAPI integration.
- WebSocket accept/close behavior, auth gate, JSON parsing, and client-specific error framing.
- Realtime call transport integration, telephony-specific connection state, and audio/TTS/STT adapters.
- Authentication dependency resolution and trial-access gates at the API boundary.

Entry adapters should not own assistant loop decisions, tool execution, memory policy, provider selection, or long-running run lifecycle rules that belong behind Gateway.

## Realtime Backend Contract

Gateway talks to assistant execution through `assistant_agent.realtime`:

- `RealtimeAgentRequest`: normalized user turn payload from Gateway.
- `RealtimeAgentEvent`: assistant-side stream events that can be mapped to Gateway frames.
- `RealtimeAgentResult`: terminal backend status, response metadata, trace/run IDs, and `expects_reply`.
- `RealtimeAgentBackend`: backend protocol implemented by `AgentGraphRealtimeBackend`.
- `RealtimeCancelToken`: cooperative cancellation token passed from Gateway to the backend.

This boundary lets Gateway preserve OpenClaw-compatible session/run semantics without making Gateway depend on `AgentGraphRuntime` internals or a legacy OpenClaw adapter.

## Current Code Map

| module | responsibility |
| --- | --- |
| `src/assistant_agent/gateway/protocol.py` | Gateway wire frame helpers, call/config constants, and supported modalities. |
| `src/assistant_agent/gateway/transport.py` | Transport-agnostic endpoint primitives for in-process tests and embedding. |
| `src/assistant_agent/gateway/ws.py` | JSON text WebSocket adapter that presents a WebSocket as a Gateway endpoint. |
| `src/assistant_agent/gateway/bridge.py` | External-client-to-session bridge: call lifecycle, frame forwarding, stale bridge eviction, disconnect cancellation, and modality gate. |
| `src/assistant_agent/gateway/session.py` | Gateway-managed session service: `message.user`, `run.cancel`, session history, active runs, interrupt, deadline, event mapping, and session manager. |
| `src/assistant_agent/gateway/event_mapping.py` | Realtime backend event to Gateway frame mapping. |
| `src/assistant_agent/gateway/ws_server.py` | Optional standalone Gateway session WebSocket server entrypoint, not the main FastAPI app route. |
| `src/assistant_agent/realtime/` | Gateway-to-assistant backend contract and `AgentGraphRealtimeBackend`. |
| `src/assistant_agent/api/gateway_runtime.py` | Process-local FastAPI-owned `GatewaySessionManager` / `GatewayBridge` boundary and shutdown cleanup. |
| `src/assistant_agent/api/gateway_websocket.py` | FastAPI entry adapters for `/ws/gateway` Gateway frames and `/ws/realtime/media` media-service events. |
| `src/assistant_agent/api/` | FastAPI HTTP/WebSocket entry adapters and product API routes. |
| `src/assistant_agent/services/assistant_run_service.py` | Shared non-Gateway assistant request/run service used by CLI, HTTP, WebSocket, eval, and demos. |
| `scripts/run_gateway_client.py` | Local operator smoke client for the Gateway frame WebSocket route. |

## OpenClaw Reference Boundary

Use `/home/lenovo1/pycharm_project/runTime` only as a reference for compatibility behavior:

- Frame names and payload semantics: `message.user`, `run.started`, `stream.chunk`, `run.end`, `run.cancel`, `ping`/`pong`, call frames, and config frames.
- Session lifecycle: active run registration, per-session history, generated IDs, reconnect, hangup grace, idle eviction, and terminal `expects_reply`.
- Cancellation and interrupt behavior.
- Transport adapter behavior and Gateway WebSocket bridging.

Do not import `openclaw_gateway_runtime`, reuse the old OpenClaw/Anthropic agent loop, or make OpenClaw adapter selection part of the current assistant runtime. If OpenClaw behavior conflicts with this document or `AGENTS.md`, this project's current architecture wins unless the user explicitly asks for a compatibility change.

## Update Rules

- Update this document when `assistant_agent.gateway`, `assistant_agent.realtime`, Gateway WebSocket transport, realtime call integration, or Gateway-related API routing changes.
- Current Gateway entry-layer implementation planning lives in `docs/development/gateway-entry-layer-development-plan.md`; that file is an execution plan, not the architecture authority.
- Keep `AGENTS.md` as the concise routing entry and this file as the Gateway-specific authority.
- Keep `.codex/skills/assistant-runtime-reference/SKILL.md` routing to this file before any legacy `runTime` reference.
- Do not put active Gateway architecture decisions only in `docs/development/**`; those files are historical plans and runbooks.
- Add or update tests in `tests/test_gateway.py`, `tests/test_gateway_session.py`, and realtime backend tests when behavior changes.

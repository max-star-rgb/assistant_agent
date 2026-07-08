# Gateway Architecture

Last updated: 2026-07-08

This document is the current canonical entry for `assistant_agent.gateway`, realtime Gateway protocol frames, entry-layer boundaries, and the Gateway-to-assistant runtime contract. Update it whenever Gateway responsibilities, realtime call behavior, Gateway WebSocket bridging, session/run/cancel semantics, or entry adapter routing changes.

## Quick Handoff

- Gateway is not a product entrypoint. CLI, Web UI, app, HTTP, WebSocket, and realtime call adapters are entry layers.
- Entry adapters may be implemented outside Python when product, transport, SDK, or deployment constraints make that preferable, but they must preserve Gateway as the authoritative lifecycle boundary and communicate through normalized Gateway frames or documented HTTP schemas.
- Gateway owns normalized message, session, run, cancel, interrupt, reconnect, hangup, and stream-frame semantics between entry layers and the assistant realtime backend.
- `assistant_agent.realtime` is the contract between Gateway and the current assistant runtime. The default adapter is `GatewayAgentAdapter`, a semantic alias of the compatibility class name `AgentGraphRealtimeBackend`.
- The realtime adapter is a thin runtime bridge. It maps realtime requests/events/results and forwards cancellation; it does not own planning, tool choice, memory policy, provider policy, agent routing, or multi-agent decisions.
- `AgentGraphRuntime` and the assistant loop remain the internal agent executor. Do not add an OpenClaw-style second agent loop.
- Web, CLI, HTTP, WebSocket, and realtime product entries should converge on Gateway ingress adapters before reaching the assistant runtime. HTTP `/agent/run`, local CLI `--text`, local CLI `--scenario` through demo flows, and legacy `/ws/agent/{session_id}` now enter Gateway through `GatewayTurnFacade`; remaining direct `AssistantRuntimeApp` callers in product entry paths are migration debt, not the target architecture.
- The main FastAPI app exposes `/ws/gateway` for normalized Gateway JSON frames and `/ws/realtime/media` for Media Relay events that are validated before being adapted into Gateway frames.
- The main FastAPI app also exposes `/agent-service/v1` as a media-service compatibility WebSocket for the vendor `message` / `sessionId` / stringified `body` protocol. That route currently returns mock `assistantControlStartAck` and `chatResponse` envelopes and does not enter the Gateway session service or assistant runtime.
- OpenClaw / `runTime` is compatibility reference material for wire protocol and lifecycle behavior only. Do not import it into this project.

## Layering

Product and transport adapters live at the entry layer:

```text
CLI / Web UI / app / HTTP route / WebSocket route / realtime call transport
        |
        v
entry adapter: auth, transport IO, product payload parsing, user experience contract
```

For realtime calls, the product path is:

```text
App / telephony SDK
        |
        v
Media Relay: STT/TTS/media references, transport details, app identity forwarding
        |
        v
/ws/realtime/media
        |
        v
Gateway lifecycle and session config boundary
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
GatewayAgentAdapter / AgentGraphRealtimeBackend compatibility name
        |
        v
AgentGraphRuntime / assistant loop
        |
        v
ActionValidator -> ToolExecutor -> ToolRegistry -> tools / providers / memory
```

The target product path, including non-realtime request/response entries, is:

```text
CLI / HTTP / Web UI / WebSocket / app
        |
        v
Gateway ingress adapter
        |
        v
GatewaySessionManager / GatewaySessionService
        |
        v
GatewayAgentAdapter
        |
        v
AssistantRuntimeApp
        |
        v
run_assistant_request
        |
        v
AgentGraphRuntime / assistant loop
```

`AssistantRuntimeApp` remains the thin backend-to-runtime boundary used by
`GatewayAgentAdapter`. Product entry layers should not construct or pass
`AgentGraphRuntime` directly, and their long-term target should not be direct
`AssistantRuntimeApp` access either. Direct app callers in product paths may
exist temporarily only as migration debt while those paths move behind
Gateway-compatible facades.

For request/response style entries, `GatewayTurnFacade` provides the in-process
sync-turn bridge: it sends a normalized `message.user` frame through
`GatewaySessionManager`, collects Gateway frames until `run.end`, and returns a
structured turn result. Endpoint-specific response schemas remain entry-adapter
concerns on top of that Gateway result.

HTTP `/agent/run` uses this bridge plus an in-process response capture id. The
Gateway runtime callback captures the full `AgentRunResponse` after
`AssistantRuntimeApp.run_request()` returns, and the HTTP route pops that
captured response after Gateway emits `run.end`. This preserves the public HTTP
schema without exposing the full HTTP response in Gateway WebSocket frames.

Local offline CLI `--text` uses the same bridge with a local
`GatewaySessionManager(start_reaper=False)` and a `GatewayAgentAdapter` callback
that captures `AssistantRunArtifacts` for CLI payload formatting. CLI
`--scenario` uses the demo matrix, and each demo scenario now runs through the
same local Gateway turn pattern before formatting the existing scenario result
payload.

Offline demo scenarios are entry-adapter smoke paths, so they should exercise
Gateway lifecycle before reaching the assistant runtime. Offline eval harnesses
are different: `scripts/run_evals.py` may call `AgentWorkflow`,
`AgentGraphRuntime`, memory retrieval, provider policy, or MCP packaging layers
directly when the eval case is measuring those lower-layer contracts. Eval
harness direct calls are allowed only as explicit offline regression probes; they
are not product entrypoint precedent.

Legacy `/ws/agent/{session_id}` uses a local Gateway manager and facade per
connection, but keeps its old external `AgentEvent` JSON stream. Its backend
callback mirrors raw runtime events to the legacy WebSocket queue while also
forwarding them to Gateway's realtime adapter, then sends the final
`agent_response` payload after Gateway emits `run.end`.

## Gateway Responsibilities

Gateway owns the protocol and lifecycle boundary for realtime or Gateway-normalized traffic:

- Accept normalized frames such as `message.user`, `run.cancel`, `ping`, `call.incoming`, `call.hangup`, and `config.update`.
- Accept validated media-entry events from `/ws/realtime/media` and adapt them to the normalized Gateway frames.
- Validate Gateway-level modality support before dispatching to the assistant backend.
- Bind or preserve `user_id`, `session_id`, `turn_id`, and `run_id`.
- Maintain per-session user text history for Gateway turns.
- Register active runs and emit `run.started`, user-visible `event.progress`, `stream.chunk`, and `run.end`.
- Include the assistant backend `trace_id` in `run.end.payload.trace_id` when available so developer/debug entry layers can load trace summaries without exposing raw provider payloads.
- Convert realtime backend events into Gateway wire frames.
- Convert backend failures into protocol-level `run.end` or `error` frames.
- Queue ordinary same-session user messages behind the active run; cancel active runs on explicit `run.cancel`, disconnect, deadline expiry, or explicit same-session interrupt.
- Cancel active runs immediately on `call.hangup` / media `session.end`, then return `call.hangup_ack`.
- Manage per-user session reuse, reconnect, hangup grace, idle eviction, and live session config.
- Treat user-message `metadata` as untrusted for system-prompt/profile selection. `system_prompt_profile`, profile-driving `channel`, and profile-driving `source` are stripped from message payload metadata; realtime phone profile selection must come from trusted Gateway/session config, not ordinary user text or arbitrary payload metadata.
- Keep external connection lifecycle separate from the assistant runtime internals.

Gateway should remain transport-agnostic where possible. WebSocket handling belongs in an adapter such as `gateway.ws` or an API entry route, while Gateway session behavior belongs in `gateway.session`.

Gateway interrupt remains a lifecycle/control concept. It should cancel or gate
the active run, preserve session continuity, and start the next turn. It should
not own semantic task revision such as merging the old goal with new
constraints, deciding whether intermediate artifacts are reusable, or resolving
committed side effects.

Realtime task state, deterministic fallback behavior, tool-wait boundaries, and interrupt/cancel handling are part of the current Gateway lifecycle contract when implemented. Keep current behavior in this document and in tests, not in archived phase plans.

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

Entry adapters may be implemented in TypeScript, Go, Rust, or another language when that better fits a Web UI, BFF, vendor WebSocket adapter, Media Relay adapter, edge deployment, or telephony/media SDK. Those non-Python layers should stay thin: parse product or transport payloads, enforce entry-layer auth and UX contracts, and forward normalized HTTP requests or Gateway frames to the Python `assistant_agent` Gateway/runtime boundary without reimplementing assistant loop, Gateway lifecycle, tool calling, memory, or provider policy.

## Media Relay WebSocket

`/ws/realtime/media` is the primary realtime call entry for Media Relay integrations. It accepts media-entry events, validates identity and session binding against the WebSocket query/auth context, and maps valid events to Gateway frames:

The Web demo may expose a Realtime Call Debugger for local Media Relay testing, but it must not add a separate Web realtime/runtime mode or make the browser a second primary Gateway client path.

| media event | required shape | Gateway mapping |
| --- | --- | --- |
| `session.start` | `session_id` from event/payload/query; optional `call_id`; optional `payload.config` | `call.incoming`; creates or resumes Gateway session and freezes session config |
| `transcript.final` | `text`, `audio_id`, `video_ids`, or `image_ids`; optional `interrupt=true` or `metadata.control=interrupt` | `message.user`; ordinary turns queue behind the active run, explicit interrupt cancels the active run and starts the new turn |
| `run.cancel` | `session_id` or `run_id` from event/payload/query | `run.cancel`; cooperative cancellation of the active run |
| `config.update` | non-empty `config` object | `config.update`; updates live session config before future turns |
| `session.end` | `session_id` from event/payload/query | `call.hangup`; cancels the active run and emits `call.hangup_ack` |
| `ping` | no payload required | `pong` |

Invalid JSON, unsupported event types, unknown config fields, missing transcript content, identity mismatch, or session mismatch produce an `error` frame and do not enter the assistant backend.

System prompt profile selection is a session configuration concern. Realtime call entries may set trusted session config such as `system_prompt_profile=realtime_phone` and `channel=realtime_phone`; message payload metadata cannot promote a normal turn into `realtime_phone` or `final_only`.

Media Relay v1 does not stream raw audio or video through Gateway. It sends references such as `audio_id`, `video_ids`, and `image_ids`; the assistant runtime receives those references through `RealtimeAgentRequest`. STT/TTS edge metadata is kept prompt-safe: `transcript.final` may attach sanitized `media_edge` metadata for transcript/STT/TTS status, but raw audio, base64 payloads, provider raw responses, API keys, and SDK blobs are removed before the backend request is built.

TTS is also an entry-adapter concern. `assistant_agent.realtime.audio_edge.gateway_frame_to_tts_event()` can map speakable Gateway frames (`stream.chunk` and display-only `event.progress`) into prompt-safe TTS edge events. It does not invoke a TTS provider, stream audio, or change assistant runtime behavior.

## Realtime Adapter Contract

Gateway talks to assistant execution through `assistant_agent.realtime`:

- `RealtimeAgentRequest`: normalized user turn payload from Gateway.
- `RealtimeAgentEvent`: assistant-side stream events that can be mapped to Gateway frames.
- `RealtimeAgentResult`: terminal backend status, response metadata, trace/run IDs, and `expects_reply`.
- `RealtimeAgentBackend`: backend protocol implemented by `AgentGraphRealtimeBackend`.
- `RealtimeCancelToken`: cooperative cancellation token passed from Gateway to the backend.

`GatewayAgentAdapter` / `RealtimeAgentAdapter` are exported semantic names for the same thin adapter currently implemented by `AgentGraphRealtimeBackend`. The compatibility class name remains available to avoid churn in existing imports and tests.

Long-running assistant turns can emit `RealtimeAgentEvent(type="run.progress", display_only=True)` for user-visible status updates such as current work, completed step, next step, blocked state, or needed user decision. The realtime adapter applies progress throttling and idle heartbeat policy before Gateway maps those updates to `event.progress` frames; entry layers decide how to display them and should not treat them as final answer content.

This boundary lets Gateway preserve OpenClaw-compatible session/run semantics without making Gateway depend on `AgentGraphRuntime` internals, `AgentRouter` internals, worker agent contracts, or a legacy OpenClaw adapter. If multi-agent realtime behavior is needed, the realtime turn must enter the main `AgentGraphRuntime` / assistant loop first; that main runtime can then delegate through the tool-governed agent communication boundary. Do not teach worker agents Gateway frames such as `call.incoming`, `call.hangup`, or WebSocket payloads.

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
| `src/assistant_agent/realtime/` | Gateway-to-assistant adapter contract, `GatewayAgentAdapter` semantic alias, and `AgentGraphRealtimeBackend` compatibility class. |
| `src/assistant_agent/realtime/audio_edge.py` | Prompt-safe helper for entry adapters that convert speakable Gateway text frames into TTS edge events without invoking a provider. |
| `src/assistant_agent/api/gateway_runtime.py` | Process-local FastAPI-owned `GatewaySessionManager`, `GatewayBridge`, `GatewayTurnFacade`, HTTP response capture, and shutdown cleanup. |
| `src/assistant_agent/api/gateway_websocket.py` | FastAPI entry adapters for `/ws/gateway` Gateway frames and `/ws/realtime/media` media-service events. |
| `src/assistant_agent/api/agent_service_websocket.py` | FastAPI compatibility adapter for the vendor `/agent-service/v1` media protocol; parses `message` / `sessionId` / stringified `body` and returns mock envelopes without entering Gateway runtime. |
| `src/assistant_agent/api/` | FastAPI HTTP/WebSocket entry adapters and product API routes. |
| `src/assistant_agent/services/gateway_turn_facade.py` | In-process sync-turn facade for request/response entries that need Gateway lifecycle semantics without a WebSocket transport. |
| `src/assistant_agent/services/assistant_runtime_app.py` | Backend-to-runtime boundary used behind `GatewayAgentAdapter`; owns the internal runtime reference without becoming the target product entry boundary. |
| `src/assistant_agent/services/assistant_run_service.py` | Shared assistant request/run service used behind `AssistantRuntimeApp`, plus eval and demo utilities. |
| `scripts/run_demo_flows.py` | Offline demo/scenario entry adapter that runs scenarios through a local `GatewayTurnFacade` and formats the existing demo summary payload. |
| `scripts/run_gateway_client.py` | Local operator smoke client for the Gateway frame WebSocket route. |
| `scripts/realtime_media_client.py` | Local Media Relay protocol smoke client for `/ws/realtime/media` scenarios. |

## OpenClaw Reference Boundary

Use `/home/lenovo1/pycharm_project/runTime` only as a reference for compatibility behavior:

- Frame names and payload semantics: `message.user`, `run.started`, `stream.chunk`, `run.end`, `run.cancel`, `ping`/`pong`, call frames, and config frames.
- Session lifecycle: active run registration, per-session history, generated IDs, reconnect, hangup grace, idle eviction, and terminal `expects_reply`.
- Cancellation and interrupt behavior.
- Transport adapter behavior and Gateway WebSocket bridging.

Do not import `openclaw_gateway_runtime`, reuse the old OpenClaw/Anthropic agent loop, or make OpenClaw adapter selection part of the current assistant runtime. If OpenClaw behavior conflicts with this document or `AGENTS.md`, this project's current architecture wins unless the user explicitly asks for a compatibility change.

## Update Rules

- Keep current Gateway protocol, lifecycle, adapter, and entry-layer decisions in this file.
- Keep `AGENTS.md` as the concise routing entry and this file as the Gateway-specific authority.
- Keep `.codex/skills/assistant-runtime-reference/SKILL.md` routing to this file before any legacy `runTime` reference.
- Do not put active Gateway architecture decisions only in `docs/development/**`; retained development files are runbooks or explicitly named execution material.

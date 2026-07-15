# Gateway Architecture

Last updated: 2026-07-15

This document is the current canonical entry for `assistant_agent.gateway`, realtime Gateway protocol frames, entry-layer boundaries, and the Gateway-to-assistant runtime contract. Update it whenever Gateway responsibilities, realtime call behavior, Gateway WebSocket bridging, session/run/cancel semantics, or entry adapter routing changes.

## Quick Handoff

- Gateway is not a product entrypoint. CLI, Web UI, app, HTTP, WebSocket, and realtime call adapters are entry layers.
- Entry adapters may be implemented outside Python when product, transport, SDK, or deployment constraints make that preferable, but they must preserve Gateway as the authoritative lifecycle boundary and communicate through normalized Gateway frames or documented HTTP schemas.
- Gateway owns normalized message, session, run, cancel, interrupt, reconnect, hangup, and stream-frame semantics between entry layers and the assistant realtime backend.
- Every accepted `message.user` receives stable Gateway-owned `turn_id` and `run_id` values at ingress. A queued turn is a cancellable lifecycle object, not an anonymous pending payload.
- Ordinary same-session turns use bounded FIFO followup queues. Session heads compete through one process-local admission controller, which bounds total queued turns and active backend runs without allowing same-session backend overlap.
- Realtime media may opt into a separate bounded semantic-interrupt control plane. Explicit media control still interrupts immediately; implicit utterances are classified in parallel while the active backend continues, and only a matching `expected_run_id` decision may change Gateway lifecycle.
- `assistant_agent.realtime` is the contract between Gateway and the current assistant runtime. The default adapter is `GatewayAgentAdapter`, a semantic alias of the compatibility class name `AgentGraphRealtimeBackend`.
- The realtime adapter is a thin runtime bridge. It maps realtime requests/events/results and forwards cancellation; it does not own planning, tool choice, memory policy, provider policy, agent routing, or multi-agent decisions.
- `AgentGraphRuntime` and the assistant loop remain the internal agent executor. Do not add an OpenClaw-style second agent loop.
- Durable structured tasks are a separate post-acceptance lifecycle owned by `DurableTaskService` and its worker. Gateway owns only the ingress turn that accepts and returns the task handle; it does not keep the durable task as an active Gateway run.
- Web, CLI, HTTP, WebSocket, and realtime product entries should converge on Gateway ingress adapters before reaching the assistant runtime. HTTP `/agent/run`, local CLI `--text`, and local CLI `--scenario` through demo flows enter Gateway through `GatewayTurnFacade`; remaining direct `AssistantRuntimeApp` callers in product entry paths are migration debt, not the target architecture.
- The main FastAPI app exposes `/ws/gateway` for normalized Gateway JSON frames and `/ws/realtime/media` for Media Relay events that are validated before being adapted into Gateway frames.
- The main FastAPI app also exposes `/agent-service/v1` as a media-service compatibility WebSocket for the vendor `message` / optional `sessionId` / stringified `body` protocol. It accepts the media-side `assistantControl`, `chat`, `audio`, `video`, and `interrupt` messages, keeps legacy `assistantControlStart` compatibility, routes `chat` through Gateway, and treats raw `audio` / `interrupt` frames as entry-layer ACK traffic. Self-contained H.264 I-frame `video` messages are decoded into a bounded JPEG context; a governed background observer updates rolling semantics and later `chat` turns inject that snapshot into the first foreground LLM context without exposing the video tool to that LLM.
- The old browser Web Chat console, `/demo/console`, `/static/index.html`, `scripts/run_client.py`, and legacy `/ws/agent/{session_id}` event stream are removed from the product app. Do not reintroduce ordinary chat entrypoints before the realtime assistant runtime is stable.
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

Vendor `/agent-service/v1` also uses a local Gateway manager and facade per
connection, but keeps the vendor `message` / optional `sessionId` / stringified
`body` envelope. Its Gateway session uses the trusted `realtime_phone` profile
and a fixed foreground tool set: `web_search`, `product_search`,
`price_compare`, `memory_retrieval`, and `memory_save`. This qualification is
derived from trusted session config, never user text. `assistantControl`
validates and records media control state,
and the legacy `assistantControlStart` handshake remains accepted for older
clients. `chat` maps the latest `speechContent` to a Gateway turn. With
`stream=true`, committed provider-token `stream.chunk` frames become media
`chatResponse` delta packets (`PROCESSING`, increasing sequence,
`final=false`), followed by one complete terminal packet (`SUCCESS`,
`final=true`). With `stream=false`, or when no provider token delta exists,
only the successful terminal packet is sent. `deliveryId` and application ACK
apply only to a successful terminal packet. A streamed failure closes with
`code=FAIL` and body-level `final=true`, but has no `deliveryId` and is not
ACK-negotiated; after a successful socket send its delivery state remains
`failed`. The safe `final_response_sent` diagnostic means either a `SUCCESS` or
`FAIL` terminal was handed to the WebSocket, not that the business turn
succeeded. `audio` and `interrupt` are accepted as transport
compatibility messages and acknowledged at the entry layer. `video` accepts
independently decodable H.264 Annex-B frames, decodes them to a three-frame
JPEG window plus a bounded local grayscale fingerprint, and attaches the stable
session video reference to later chat turns. A per-connection observer applies
adaptive sampling, pixel difference, SSIM, and local histogram change detection;
selected frames enter a latest-wins background queue. Background understanding
still runs through `ActionValidator -> ToolExecutor -> video_understanding`, but
`video_understanding` is not exposed to the foreground Agent-Service model.
The entry adapter does not call the video provider directly.

The runtime owns a bounded semantic snapshot per opaque `video_id`. Immediately
before every Agent-Service model context build it projects the latest snapshot
into the independent `realtime_video_context` section. Thus the first DeepSeek
decision can use completed Qwen observations without a foreground tool round
trip. A narrow current-camera reference targets the latest decoded frame
sequence and may wait up to 4.0 seconds for the shared observer snapshot to
reach that sequence. If no equal/newer work is represented, the latest frame is
promoted through the same governed queue. Timeout injects `refreshing` or
`stale` state with a sequence gap and never starts a foreground or second Qwen
call. Frame freshness uses capture age; snapshot publication age remains a
separate diagnostic. Ordinary non-Agent-Service video/API requests retain
the explicit `video_understanding` tool and `recent_frame_fallback` behavior.
The raw window is 3 frames, semantic retention is 8 keyframes, and observer
concurrency is one in flight plus one latest pending frame.

Vendor chat execution is detached from the WebSocket receive loop, so a long
Gateway turn does not prevent later raw media frames from being validated and
acknowledged. Optional `clientCapabilities` negotiate prompt-free
`chatProgress` and application-level `chatResponseAck`. Media `chat.stream`
independently selects delta delivery; clients that send `stream=false` or omit
it retain single-final-response behavior. Provider-token streaming is enabled
for `stream=true`, while the runtime commit barrier continues to suppress
tool-call preambles. Per-packet receive/send logs are DEBUG. Connection open/close,
turn latency, ACK, and failures remain INFO/WARNING, with one close INFO carrying
message/video/byte/failure counters.

## Gateway Responsibilities

Gateway owns the protocol and lifecycle boundary for realtime or Gateway-normalized traffic:

- Accept normalized frames such as `message.user`, `run.cancel`, `ping`, `call.incoming`, `call.hangup`, and `config.update`.
- Accept validated media-entry events from `/ws/realtime/media` and adapt them to the normalized Gateway frames.
- Validate Gateway-level modality support before dispatching to the assistant backend.
- Bind or preserve `user_id`, `session_id`, `turn_id`, and `run_id`.
- Maintain per-session user text history for Gateway turns.
- Register active runs and emit `run.started`, user-visible `event.progress`, `stream.chunk`, and `run.end`.
- Include the assistant backend `trace_id` in `run.end.payload.trace_id` when available so developer/debug entry layers can load trace summaries without exposing raw provider payloads.
- For cancelled turns, include prompt-safe cancel metadata in `run.end.payload.cancel` (`source`, optional `reason`, `phase`, `best_effort`, and `deadline_ms` when applicable). If Gateway ends the turn before a backend trace is available, include `run.end.payload.trace.status=not_available` with reason `cancelled_before_backend_result` instead of inventing a trace id.
- Convert realtime backend events into Gateway wire frames.
- Convert backend failures into protocol-level `run.end` or `error` frames.
- Queue ordinary same-session user messages behind the active run; apply per-session and process-wide limits; cancel either queued or active runs on explicit `run.cancel`, disconnect, deadline expiry, explicit same-session interrupt, or queue-wait expiry.
- Cancel active runs immediately on `call.hangup` / media `session.end`, then return `call.hangup_ack`.
- Treat cancel/interrupt as a first-class realtime turn outcome. After cancel, old run output is not speakable or user-visible; late backend/tool results may be retained only as trace or stale artifacts.
- Manage per-user session reuse, reconnect, hangup grace, idle eviction, and live session config.
- Treat user-message `metadata` as untrusted for system-prompt/profile selection. `system_prompt_profile`, profile-driving `channel`, and profile-driving `source` are stripped from message payload metadata; realtime phone profile selection must come from trusted Gateway/session config, not ordinary user text or arbitrary payload metadata.
- Keep external connection lifecycle separate from the assistant runtime internals.

Gateway should remain transport-agnostic where possible. WebSocket handling belongs in an adapter such as `gateway.ws` or an API entry route, while Gateway session behavior belongs in `gateway.session`.

Gateway interrupt remains a lifecycle/control concept. It should cancel or gate
the active run, preserve session continuity, and start the next turn. It should
not own semantic task revision such as merging the old goal with new
constraints, deciding whether intermediate artifacts are reusable, or resolving
committed side effects. The optional `RealtimeTurnArbiter` is a separate semantic
classifier; Gateway validates and applies its structured disposition but does
not move business planning or tool decisions into the entry layer.

## Queue and Admission Contract

Gateway QueuePolicy v1 separates two constraints:

```text
message.user -> per-session FIFO -> process-wide admission -> backend run
                 one head only       bounded/fair FIFO        active permit
```

- Default mode is `followup`. Explicit interrupt remains a control operation,
  but the replacement turn does not start its backend until the cancelled
  backend has actually exited and released its permit.
- Default limits are 8 pending turns per session, 64 accepted queued turns
  process-wide, 4 active backend runs, and 120 seconds of total queue wait.
  Overflow rejects the newest message; Gateway never silently drops or merges
  an accepted user message.
- Queue time starts at ingress and includes both same-session waiting and global
  capacity waiting. It does not consume the backend run deadline and queued
  text is appended to session history only after admission.
- `run.cancel` can target a queued `run_id`. Queue timeout and pre-run cancel
  end with `run.end(reason=cancelled)` plus prompt-safe cancellation metadata
  with `phase=before_llm`; neither path calls the backend.
- `run.queued` reports `reason=session_busy|global_capacity`, queue depth, global
  queue depth, and the ingress timestamp. `queue_overflow`, `identity_conflict`,
  and `duplicate_message` are structured `error` codes.
- Retry identity is scoped to a user/session and may use `client_message_id`,
  `turn_id`, or `run_id`. Equal replays are acknowledged as duplicates; reuse
  with a different payload is rejected. The index is bounded by TTL and entry
  count and stores only fingerprints and identifiers.
- Lifecycle observation records `gateway.run.queued`,
  `gateway.run.admitted`, `gateway.run.queue_rejected`, and
  `gateway.run.queue_expired` in addition to existing run events. Payloads are
  bounded metrics/reasons and never include user text.

The policy is configured at process startup with strict positive values:

| environment variable | default |
| --- | ---: |
| `MULTIMODAL_AGENT_GATEWAY_MAX_ACTIVE_RUNS` | `4` |
| `MULTIMODAL_AGENT_GATEWAY_MAX_PENDING_PER_SESSION` | `8` |
| `MULTIMODAL_AGENT_GATEWAY_MAX_QUEUED_TURNS` | `64` |
| `MULTIMODAL_AGENT_GATEWAY_QUEUE_WAIT_TIMEOUT_MS` | `120000` |
| `MULTIMODAL_AGENT_GATEWAY_DEDUPE_TTL_S` | `300` |
| `MULTIMODAL_AGENT_GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER` | `1024` |

All Gateway queue, dedupe, and admission state is process-local and in memory. It does
not provide restart recovery, cross-worker consistency, durable-task storage, message
collection/summarization, or live prompt steering. The retained
[design](superpowers/specs/2026-07-13-gateway-queue-admission-design.md) and
[implementation plan](superpowers/plans/2026-07-13-gateway-queue-admission.md)
record the reviewed rationale and execution evidence; this document remains the
current architecture authority.

## Gateway and durable task separation

When `MULTIMODAL_AGENT_DURABLE_TASKS_ENABLED=true`, an `/agent/run` or other Gateway-backed ingress turn may ask the native model to submit `task_plan_submit`. A successful submission is terminal for that Gateway turn:

```text
Gateway message.user
  -> one normal Gateway run
  -> AgentGraphRuntime
  -> task_plan_submit
  -> response data.task{submission_status, task_id, task_status, progress_url}
  -> Gateway run.end(completed)

later, outside Gateway run lifecycle:
DurableTaskWorker -> lease -> one quantum -> checkpoint
```

The durable `task_id` is not a Gateway `run_id`, is not inserted into the Gateway active-run map, and does not reuse the in-memory Gateway followup queue. Progress and control use the identity-scoped HTTP API:

- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events?after=<cursor>&limit=<n>`
- `POST /tasks/{task_id}/confirmations`
- `POST /tasks/{task_id}/input`
- `POST /tasks/{task_id}/cancel`

The API derives identity from the authenticated context (or explicit local/offline query identity), never from write-body `user_id`. Public projections omit lease tokens, confirmation/input digests, binding digests and step idempotency keys; pending confirmations include a bounded, credential-key-redacted summary of the final tool arguments so approval is not blind. FastAPI lifespan reuses the runtime-owned service, optionally starts one cooperative worker, stops it before Gateway shutdown, and closes the SQLite store once.

SQLite tasks survive app restart and expired leases can be reclaimed, but this is not a distributed queue or exactly-once protocol. A step attempt is committed before the external call; expired read-only attempts may retry within budget, while possible writes with uncertain commit state stop at `outcome_unknown` for operator/user resolution. API cancellation also raises a process-local cooperative task token; cross-process cancellation is outside the single-host first-version boundary.

## Realtime Semantic Interrupt Arbitration

Realtime calls distinguish two interrupt sources:

- `explicit_control`: a user button, Media Relay control, `interrupt=true`,
  `control=interrupt|barge_in|cancel_previous`, `run.cancel`, or hangup. Gateway
  applies these signals immediately and never waits for an LLM.
- `semantic_llm`: an ordinary final transcript whose meaning may cancel, revise,
  or replace the active task. A separate `RealtimeTurnArbiter` classifies this
  relation without starting a second business runtime.

Semantic arbitration is eligible only when all of the following hold:

1. the process feature flag is enabled;
2. the trusted entry capability is `supports_semantic_interrupt=true`;
3. the session has an active backend run;
4. the new turn is not already an explicit interrupt;
5. the trusted session config has not set `semantic_interrupt_enabled=false`.

Only `REALTIME_MEDIA_ENTRY_CAPABILITIES` declares this capability. Generic
`/ws/gateway`, HTTP/CLI turns, and the agent-service compatibility entry do not.

The control-plane flow is:

```text
ordinary realtime transcript while run R1 is active
    |
    |-- accepted as queued turn R2; no backend permit consumed
    |-- Media Relay may already pause or duck TTS
    `-- bounded RealtimeTurnArbitrationController
            |
            v
       RealtimeTurnArbiter(task-state snapshot, new utterance)
            |
            v
       validated disposition + expected_run_id=R1
            |
            v
       Gateway compare-and-apply
            |-- FOLLOWUP / UNCERTAIN -> keep FIFO; R1 continues
            |-- ACK_NOOP             -> complete R2 without backend; R1 continues
            |-- CANCEL_ONLY          -> cancel R1; complete R2 without backend
            `-- REVISE / REPLACE     -> cancel R1; start R2 only after R1 exits
```

The normalized dispositions are:

| disposition | active run | accepted new turn |
| --- | --- | --- |
| `FOLLOWUP` | continue | remain in FIFO |
| `UNCERTAIN` | continue | conservative FIFO fallback |
| `ACK_NOOP` | continue | `run.end(completed)`, no backend |
| `CANCEL_ONLY` | cancel | `run.end(completed)`, no backend |
| `REVISE_ACTIVE` | cancel | move to replacement head with a validated revision |
| `REPLACE_ACTIVE` | cancel | move to replacement head with `change_goal` |

Low confidence, timeout, Provider error, invalid JSON, control-plane saturation,
or a stale `expected_run_id` never cancels the current run. A still-accepted
stale turn becomes an ordinary followup; a cancelled/expired turn discards the
late decision. Timed-out synchronous Provider work retains its bounded control
slot until the underlying call actually returns, preventing hidden background
thread accumulation.

The arbiter receives only the new utterance and a bounded projection containing
the active objective, recent constraints, pending-tool status, TTS state, and
committed-side-effect count. It has no tools, long-term memory, agent routing,
artifact details, raw tool results, media bytes, or Provider payloads. Lifecycle
events record decision ids, disposition, confidence bucket, reason code, latency
and match/fallback facts, never utterance or prompt text.

`REVISE_ACTIVE` and `REPLACE_ACTIVE` do not inject text into the currently
executing `AgentGraphRuntime`. The current runtime receives only cooperative
cancellation. The replacement run receives normalized arbitration metadata and
the existing realtime task-state snapshot. `change_goal` clears old constraints
and stales old artifacts while retaining side-effect records; cancellation
cannot roll back committed external effects.

Media Relay owns immediate audio experience. It may pause or duck TTS when the
user speaks and later resume or supersede output based on the decision. The
Python Gateway owns text/run visibility after cancellation but does not pretend
to control a TTS provider that is outside this repository.

The process policy is configured with strict values:

| environment variable | default |
| --- | ---: |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_ENABLED` | `false` |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_TIMEOUT_MS` | `1000` |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MAX_CONCURRENCY` | `2` |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MIN_CONFIDENCE` | `0.80` |

Default mock/local/offline profiles use a deterministic `UNCERTAIN` fallback.
An actual LLM arbitration call additionally requires `provider_smoke` or
`pilot` and a non-mock, configured chat adapter; the presence of an API key does
not enable it. The control-plane call can share Provider resources with the
business runtime, so zero indirect resource contention is a metric target, not
an assumed guarantee.

Live prompt steering remains out of scope. Supporting in-place active-run goal
changes would require a separate runtime mailbox, safe checkpoints, context
revision ordering, tool commit barriers and output versioning.

Realtime turn cancellation metadata is normalized through
`RealtimeTurnCancellationContract`:

```text
cancelled_by = interrupt | run.cancel | hangup | disconnect | deadline
phase = before_llm | llm_streaming | tool_running | final_streaming | tts_playing
stale_outputs = true
can_reuse_tool_result = false
speakable = false
```

Current text-only realtime v1 does not invoke or manage a TTS provider, but it
still uses `speakable=false` as the outbound text gate for entry adapters. The
Gateway cancel token, realtime backend result metadata, `run.end.payload.cancel`,
and ToolExecutor cancellation result data all preserve the same prompt-safe
contract while retaining legacy `cancel_source`, `cancel_reason`, `cancel_phase`,
and `deadline_ms` fields for compatibility.

Realtime task state, deterministic fallback behavior, tool-wait boundaries, and interrupt/cancel handling are part of the current Gateway lifecycle contract when implemented. Keep current behavior in this document and in tests, not in archived phase plans.

### Proactive delivery activity snapshot

`GatewaySessionService.has_active_run()` and
`GatewaySessionManager.has_active_run(user_id)` expose read-only snapshots for
proactive delivery deferral. These queries inspect existing session/run state;
they do not create or touch sessions, and proactive work cannot use them to
interrupt an active run.

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

## Entry Identity and Session Rules

Gateway entry adapters must bind identity before a user turn reaches the assistant backend:

- HTTP `/agent/run` resolves authenticated request identity at the route boundary, then runs the turn through `GatewayTurnFacade` with the resolved `user_id` and `session_id`.
- Gateway WebSocket `/ws/gateway` resolves the WebSocket identity from auth/query context, rejects mismatched frame `user_id` or `session_id`, and injects trusted `source=gateway_websocket` metadata only after the frame passes that check.
- Media Relay WebSocket `/ws/realtime/media` requires a bound session for non-`ping` events, maps media events into normalized Gateway frames, and injects trusted `source=realtime_media_websocket` metadata only at the adapter boundary.
- Vendor `/agent-service/v1` preserves the vendor envelope at the entry layer, but `chat` turns use a local `GatewayTurnFacade`. Raw `audio` and `interrupt` remain entry-layer ACK traffic. Raw self-contained H.264 `video` is validated and decoded at the entry boundary into a bounded local frame context; only its stable `video_id` is promoted on a later Gateway chat turn.

Entry adapters may attach prompt-safe `entry_capabilities` metadata so downstream code can distinguish text streaming, interrupt support, TTS state support, realtime task-state support, media reference support, raw media support, TTS edge event support, and App shopping-detail presentation without inferring behavior from transport names. These capability declarations are informational; they do not authorize tool calls, provider selection, memory access, or new modalities.

`supports_shopping_detail_v1=true` is injected only by the authenticated ordinary Gateway WebSocket entry. HTTP, CLI, Agent Service, realtime media, and phone entries leave it false. For this entry the realtime backend buffers model response deltas until the terminal result is known. After a successful governed `price_compare`, it discards those deltas, selects the first successful `ToolResult` that can produce eligible cards, and emits exactly one deterministic `stream.chunk` containing the natural-language summary plus the single `<detail>` protocol block; later model results cannot overwrite that original presentable result. If shopping presentation is not activated, buffered deltas are forwarded normally before the final event. Conversation history and `AgentResponse.message` retain only the natural-language summary; protocol tags are entry presentation and never become assistant context. The presenter omits offers without a valid price, HTTP(S) product link, or HTTP(S) image and renders at most three eligible offers; an empty eligible set produces only natural language and no empty `<detail>`.

Realtime task-state is opt-in at the request/capability level. Ordinary Gateway metadata, `source=gateway_*`, or a `realtime.run_id`/`turn_id` pair does not by itself enable phone/realtime task semantics. Realtime call adapters such as `/ws/realtime/media` and `/agent-service/v1` declare `supports_realtime_task_state=true`; ordinary request/response chat facades should leave that capability false unless they explicitly want realtime interruption, pending-tool, TTS/display, and artifact-reuse behavior.

## Hermes-Inspired Boundaries

Hermes' message gateway is useful reference material for defensive edge handling, but this project does not adopt its multi-IM runtime shape. Borrowed ideas should stay within the current Gateway boundaries:

- Session isolation maps to explicit `user_id`, `session_id`, trusted `source`, and session config handling, not platform-specific session-key factories.
- Running-agent control interception maps to Gateway control frames: `run.cancel`, explicit interrupt metadata, deadline cancellation, disconnect cancellation, and `call.hangup`.
- Adapter capability fallback maps to small entry capability declarations and outbound formatters, not platform-specific send APIs in the Gateway core.
- Hook-style lifecycle visibility maps to controlled Gateway lifecycle events and trace/observability records, not arbitrary user hook execution in the Gateway process.
- Platform formatting, slash commands, memory flush, and external delivery routing remain outside Gateway unless implemented as thin entry adapters that forward normalized Gateway frames.

## Media Relay WebSocket

`/ws/realtime/media` is the primary realtime call entry for Media Relay integrations. It accepts media-entry events, validates identity and session binding against the WebSocket query/auth context, and maps valid events to Gateway frames:

Local Media Relay testing should use `scripts/realtime_media_client.py` or `scripts/run_realtime_call_simulator.py`; see `docs/development/realtime-runtime-operator-runbook.md` for the operator loop. A future Web UI can be added as a thin entry adapter, but it must not reintroduce a separate browser chat runtime or make the browser a second primary Gateway client path.

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

Phase 1 realtime work in this repository is text orchestration only. The Media Relay or upstream media service owns ASR, TTS, VAD, telephony SDK state, audio transport, and playback. `assistant_agent` accepts finalized text events, maps them into Gateway lifecycle frames, runs the existing assistant runtime, and emits text Gateway frames that an entry adapter may pass to TTS. The local gate for this contract is `scripts/run_realtime_call_simulator.py`, which runs `basic`, `interrupt`, `hangup`, `cancel`, and `tool_interrupt` scenarios in process without a server, real provider, audio bytes, or media refs.

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

Realtime event projection carries stable delivery semantics without moving control flow out of the assistant loop:

| runtime event | Gateway frame | speech policy | persistence | replacement behavior |
| --- | --- | --- | --- | --- |
| `response.chunk` | `stream.chunk` | `required` | `final` | supersedes the run progress slot |
| `run.progress` | `event.progress` | `optional` | `ephemeral` | replaceable at `<run_id>:progress` |
| `tool.started` / `tool.finished` / `tool.failed` | `event.tool` | `never` | `ephemeral` | not replaceable |
| `confirmation.required` | `confirmation.required` | `required` | `ephemeral` | waits for a user reply |

Every `run.end` supersedes the same progress slot, including completed, failed, and cancelled runs. This lets an entry adapter remove already displayed progress after a final answer or cancellation. Progress and tool lifecycle events remain display/trace state; they are not assistant final text and must not be promoted into conversation history or long-term memory.

Provider text remains provisional until the current native LLM call is known not to contain tool calls. The runtime buffers streamed text for every tool-capable iteration, discards it when that iteration returns tool calls, and flushes it only when the iteration resolves as a final answer. This commit barrier prevents tool-call preambles from becoming `stream.chunk` output while keeping streaming as an event projection rather than the assistant loop itself.

The repository still does not invoke a TTS provider. `speech_policy`, `persistence`, `replaceable`, `replacement_key`, and `supersedes` are prompt-safe entry-layer facts that UI or future audio adapters can consume. Pending tool confirmation is projected as `tool.finished` followed by `confirmation.required`, not as a misleading completed progress message.

This boundary lets Gateway preserve OpenClaw-compatible session/run semantics without making Gateway depend on `AgentGraphRuntime` internals, `AgentRouter` internals, worker agent contracts, or a legacy OpenClaw adapter. If multi-agent realtime behavior is needed, the realtime turn must enter the main `AgentGraphRuntime` / assistant loop first; that main runtime can then delegate through the tool-governed agent communication boundary. Do not teach worker agents Gateway frames such as `call.incoming`, `call.hangup`, or WebSocket payloads.

## Current Code Map

| module | responsibility |
| --- | --- |
| `src/assistant_agent/gateway/protocol.py` | Gateway wire frame helpers, call/config constants, and supported modalities. |
| `src/assistant_agent/gateway/capabilities.py` | Prompt-safe entry adapter capability declarations used in Gateway metadata. |
| `src/assistant_agent/gateway/observability.py` | Controlled fail-open Gateway lifecycle event model and sink helper. |
| `src/assistant_agent/gateway/queueing.py` | Bounded queue policy, process-local fair run admission, stable queued-turn records, and TTL/LRU retry identity index. |
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
| `src/assistant_agent/api/agent_service_websocket.py` | FastAPI compatibility adapter for the vendor `/agent-service/v1` media protocol; preserves `message` / optional `sessionId` / stringified `body` envelopes, accepts media `assistantControl` / `chat` / `audio` / `video` / `interrupt`, ingests self-contained H.264 video frames, and routes chat plus stable video references through a local `GatewayTurnFacade`. |
| `src/assistant_agent/services/h264_video_ingestion.py` | Entry-layer H.264 validation, bounded FFmpeg I-frame decode, JPEG artifact lifecycle, and registration in the runtime-owned `VideoContextStore`; never calls an understanding provider. |
| `src/assistant_agent/services/realtime_video_observer.py` | Per-connection local adaptive selection, retained-keyframe lifecycle, latest-wins background scheduling, and governed `video_understanding` execution. |
| `src/assistant_agent/services/realtime_video_memory.py` | Runtime-owned bounded prompt-safe semantic video snapshots, health/failure state, and per-video isolation. |
| `src/assistant_agent/api/` | FastAPI HTTP/WebSocket entry adapters and product API routes. |
| `src/assistant_agent/services/gateway_turn_facade.py` | In-process sync-turn facade for request/response entries that need Gateway lifecycle semantics without a WebSocket transport. |
| `src/assistant_agent/services/assistant_runtime_app.py` | Backend-to-runtime boundary used behind `GatewayAgentAdapter`; owns the internal runtime reference without becoming the target product entry boundary. |
| `src/assistant_agent/services/assistant_run_service.py` | Shared assistant request/run service used behind `AssistantRuntimeApp`, plus eval and demo utilities. |
| `scripts/run_demo_flows.py` | Offline demo/scenario entry adapter that runs scenarios through a local `GatewayTurnFacade` and formats the existing demo summary payload. |
| `scripts/run_gateway_client.py` | Local operator smoke client for the Gateway frame WebSocket route. |
| `scripts/realtime_media_client.py` | Local Media Relay protocol smoke client for `/ws/realtime/media` scenarios. |
| `scripts/run_realtime_call_simulator.py` | In-process text-only realtime call simulator for Phase 1 Gateway lifecycle gates. |

### Entry Convergence Inventory

Phase 0 treats these entry classifications as architecture contracts:

| Entry | Current path | Classification |
| --- | --- | --- |
| HTTP `POST /agent/run` | `routes_agent.run_agent -> _run_agent_through_gateway -> GatewayTurnFacade -> GatewaySessionManager -> GatewayAgentAdapter -> AssistantRuntimeApp -> run_assistant_request -> AgentGraphRuntime` | Canonical Gateway-first product entry. |
| Gateway WS `/ws/gateway` | `gateway_websocket -> get_gateway_bridge().bridge(...) -> GatewaySessionManager` | Canonical normalized Gateway entry. |
| Realtime media WS `/ws/realtime/media` | media event validation -> Gateway frame mapper -> `get_gateway_bridge().bridge(...)` | Canonical realtime entry adapter. |
| Local CLI `--text` | local `GatewaySessionManager + GatewayTurnFacade + GatewayAgentAdapter` | Canonical local Gateway-first entry. |
| CLI `--scenario` | demo matrix through local `GatewayTurnFacade` in `scripts/run_demo_flows.py` | Offline demo adapter, Gateway-first internally. Do not expand into product behavior. |
| Vendor `/agent-service/v1` | vendor `message` / optional `sessionId` / stringified `body` protocol; `assistantControl` is the media handshake, legacy `assistantControlStart` remains accepted, raw H.264 I-frames become a bounded local JPEG context, and `chat` uses local `GatewayTurnFacade` with the stable session video reference | Compatibility vendor surface, Gateway-first internally for chat and video references. H.264 decode stays at the entry boundary; tool choice and provider calls stay in the assistant runtime. |
| HTTP `POST /agents/run` | explicit `AgentRouter` service call | Separate opt-in router/debug entry, not the default product path. |
| Inbound A2A `/a2a/rpc` | protocol adapter over `AgentRouter` | Explicit adapter, not Gateway lifecycle. |
| MCP `tool_run` | `ActionValidator -> ToolExecutor -> ToolRegistry` | Tool adapter path, not assistant entry. |
| Removed legacy Web Chat | `/demo/console`, `/static/index.html`, `/ws/agent/{session_id}`, and `scripts/run_client.py` are not registered or shipped | Ordinary browser chat is out of scope until the realtime assistant runtime is stable. |

Phase 0 entry convergence tests live in `tests/test_phase0_entrypoint_contracts.py`.
Gateway lifecycle invariants for active-run hangup, inactive-run hangup, trusted entry source, and text-only realtime media events live in `tests/test_gateway.py`, `tests/test_gateway_api.py`, and `tests/test_realtime_call_simulator.py`.

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

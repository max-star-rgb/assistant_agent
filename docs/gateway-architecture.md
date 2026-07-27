# Gateway Architecture

Last updated: 2026-07-27

This document is the current canonical entry for `assistant_agent.gateway`, realtime Gateway protocol frames, entry-layer boundaries, and the Gateway-to-assistant runtime contract. Update it whenever Gateway responsibilities, realtime call behavior, Gateway WebSocket bridging, session/run/cancel semantics, or entry adapter routing changes. Media-Agent `/agent-service/v1` wire-field details, examples, and H.264 payload constraints live in `docs/media-agent-service-websocket.md`.

## Quick Handoff

- Gateway is not a product entrypoint. CLI, Web UI, app, HTTP, WebSocket, and realtime call adapters are entry layers.
- Entry adapters may be implemented outside Python when product, transport, SDK, or deployment constraints make that preferable, but they must preserve Gateway as the authoritative lifecycle boundary and communicate through normalized Gateway frames or documented HTTP schemas.
- Gateway owns normalized message, session, run, cancel, interrupt, reconnect, hangup, and stream-frame semantics between entry layers and the assistant realtime backend.
- Every accepted `message.user` receives stable Gateway-owned `turn_id` and `run_id` values at ingress. A queued turn is a cancellable lifecycle object, not an anonymous pending payload.
- Ordinary same-session turns use bounded FIFO followup queues. Session heads compete through one process-local admission controller, which bounds total queued turns and active backend runs without allowing same-session backend overlap.
- For normalized Gateway WebSocket, one live bridge owner is allowed per `user_id` in a process. A newer connection supersedes older same-user bridges, including idle bridges that have not opened a runtime endpoint yet. Superseding a bridge is not treated as client disconnect for run lifecycle: the active Gateway run is not cancelled, and later runtime frames are delivered to the newest owner.
- A true normalized-Gateway transport disconnect moves delivery to `DETACHED` for a bounded reconnect grace period instead of immediately cancelling the active run. The session relay keeps a bounded cursor-addressable outbox; `session.resume(payload.cursor)` replays retained frames and returns `session.attached`. If no owner returns before grace expiry, Gateway cancels the then-active run with `reason=reconnect_grace_expired`.
- Realtime media may opt into a separate bounded semantic-interrupt control plane. Explicit media control still interrupts immediately; implicit utterances are classified in parallel while the active backend continues, and only a matching `expected_run_id` decision may change Gateway lifecycle.
- `assistant_agent.gateway.runtime_backend` and `runtime_types` define the contract between Gateway and the current assistant runtime. `GatewayRuntimeAdapter` is the default implementation.
- The Gateway runtime adapter is a thin bridge. It maps requests/events/results and forwards cancellation; it does not own planning, tool choice, memory policy, provider policy, agent routing, or multi-agent decisions. There is no separate top-level `realtime` package.
- `AgentGraphRuntime` and the assistant loop remain the internal agent executor. Do not add an OpenClaw-style second agent loop.
- Product-level "Agent instance" means the connection/user-owned logical `GatewaySessionService`, not a dedicated `AgentGraphRuntime` object. A logical AgentSession owns history, queued/active turns, cancellation and media/session correlation. Runtime execution remains application-owned and pooled across sessions.
- Durable structured tasks are a separate post-acceptance lifecycle owned by `DurableTaskService` and its worker. Gateway owns only the ingress turn that accepts and returns the task handle; it does not keep the durable task as an active Gateway run.
- Web, CLI, HTTP, WebSocket, and realtime product entries should converge on Gateway ingress adapters before reaching the assistant runtime. HTTP `/agent/run`, local CLI `--text`, and local CLI `--scenario` through demo flows enter Gateway through `GatewayTurnFacade`; remaining direct `AssistantRuntimeApp` callers in product entry paths are migration debt, not the target architecture.
- The main FastAPI app exposes `/ws/gateway` for normalized Gateway JSON frames and `/agent-service/v1` as the Media Service WebSocket. The vendor route preserves the `message` / optional `sessionId` / stringified `body` protocol, accepts `assistantControl`, `chat`, `audio`, `video`, and `interrupt`, keeps legacy `assistantControlStart` compatibility, routes `chat` through Gateway, treats raw `audio` as entry-layer ACK traffic, and maps `interrupt` to cancellation of the active Gateway turn plus locally queued connection-owned turns before acknowledging it. Self-contained H.264 I-frame `video` messages are decoded into a bounded JPEG context; a governed background observer pre-warms rolling semantics, while AgentRuntime may dynamically expose the unified `vision_understanding` tool for active-video turns. The main LLM knows only this single visual tool; it never receives VLM role instructions, frames, JPEG paths, base64 media, or provider raw responses. The exact Media-Agent wire contract is `docs/media-agent-service-websocket.md`.
- The old browser Web Chat console, `/demo/console`, `/static/index.html`, and legacy `/ws/agent/{session_id}` event stream are removed from the product app. `scripts/run_client.py` is only a local Media-Agent protocol console client for `/agent-service/v1`, not a browser chat runtime. It still uses the real Media-Agent compatibility route and marks `clientInfo.clientType=run_client` only for prompt-safe observability.
- OpenClaw / `runTime` is compatibility reference material for wire protocol and lifecycle behavior only. Do not import it into this project.

## Layering

Product and transport adapters live at the entry layer:

```text
CLI / Web UI / app / HTTP route / WebSocket route / realtime call transport
        |
        v
entry adapter: auth, transport IO, product payload parsing, user experience contract
```

For Media-Agent calls, the product path is:

```text
Media Service
        |
        | assistantControl / chat / audio / video(H.264) / interrupt
        v
/agent-service/v1
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
GatewayRuntimeAdapter / GatewayRuntimeAdapter compatibility name
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
GatewayRuntimeAdapter
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
`GatewayRuntimeAdapter`. Product entry layers should not construct or pass
`AgentGraphRuntime` directly, and their long-term target should not be direct
`AssistantRuntimeApp` access either. Direct app callers in product paths may
exist temporarily only as migration debt while those paths move behind
Gateway-compatible facades.

For request/response style entries, `GatewayTurnFacade` provides the in-process
sync-turn bridge: it sends a normalized `message.user` frame through
`GatewaySessionManager`, collects Gateway frames until `run.end`, and returns a
structured turn result. Endpoint-specific response schemas remain entry-adapter
concerns on top of that Gateway result.

Gateway 在入口创建的 `run_id` 会原样传入 Assistant Runtime；Gateway lifecycle、
Runtime trace 和终态 frame 共享同一个 `run_id`。Runtime-owned `trace_id` is announced
in the initial `task_started` progress event. The facade exposes this prompt-safe correlation
before `run.end` and preserves it on timeout/error exceptions. A caller timeout
therefore means the entry failed while the runtime is `pending_cancel`; it does
not synthesize a runtime failure. Gateway cancel and terminal lifecycle records
include the early correlation when known. If runtime creation was never reached,
entries report trace status as `not_available` rather than inventing an id.

HTTP `/agent/run` uses this bridge plus an in-process response capture id. The
Gateway runtime callback captures the full `AgentRunResponse` after
`AssistantRuntimeApp.run_request()` returns, and the HTTP route pops that
captured response after Gateway emits `run.end`. This preserves the public HTTP
schema without exposing the full HTTP response in Gateway WebSocket frames.

Local offline CLI `--text` uses the same bridge with a local
`GatewaySessionManager(start_reaper=False)` and a `GatewayRuntimeAdapter` callback
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
`body` envelope. Every WebSocket connection allocates a fresh internal
Agent/Gateway `session_id`; the vendor `sessionId` remains only the protocol
correlation value returned to the media side and cannot resume conversation
history from an older call. Observability/Langfuse therefore uses that internal
id as `agent_session_id` / `langfuse.session.id` and explicitly labels its scope
as `agent_service_connection`; it must not present the vendor correlation id as
a durable conversation id. Its Gateway session uses the trusted Agent-Service
entry profile without a business-tool name allowlist. Every registered read tool
enters the candidate set by the shared exposure policy; media requirements and
other structured conditions are then applied, so `vision_understanding` appears
only when active media makes it valid. Provider-backed tools such as `weather`
只有在当前运行模式已经正确注册对应 adapter 时才进入目录；
真实模式缺少 MCP mapping 或配置时仍然 fail closed。`shopping_search` performs product
search plus price comparison；其结构化结果作为 tool observation 回到下一轮 LLM，由 LLM 生成最终
购物文本。Realtime/Gateway 不执行购物展示决策，也不覆盖模型最终正文。Tool qualification is derived from trusted session
config and structured request media, never user text. `assistantControl`
validates and records media control state,
and the legacy `assistantControlStart` handshake remains accepted for older
clients. `chat` maps the latest `speechContent` to a Gateway turn. With
`stream=true`, committed provider-token `stream.chunk` frames become media
`chatResponse` delta packets (`PROCESSING`, increasing sequence,
`final=false`), followed by one terminal packet (`SUCCESS`, `final=true`) whose
`description` contains only text not already delivered by prior deltas. When all
answer text was already delivered, the terminal packet uses an empty
`description`. When at least one delta was delivered, that terminal packet is
also marked `display_only=true` / `displayOnly=true` for clients that support
that hint. With
`stream=false`, or when no provider token delta exists,
only the successful terminal packet is sent. Optional `assistantControl.clientInfo`
is classified only for observability: omitted or unknown clients are
`media_agent`, while the local script client is `run_client`. `deliveryId` and application ACK
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
首帧、明显变化帧和最长静态 2 秒到期帧进入 latest-wins 后台队列。每轮后台理解只向
Provider 发送当前选中的一张 JPEG；历史画面不作为多帧请求重发，只把上次成功语义摘要
裁剪后作为文本上下文。Background understanding still runs through
`ActionValidator -> ToolExecutor -> ToolRegistry -> vision_understanding`；视频输入
由该工具内部的视频分支处理，不存在独立公共 video ToolSpec 或 Provider 路径。
这里的成功分三层：Execution Success 只表示 `ToolResult.success is True`；
Semantic Success 表示 `VideoUnderstandingResult` 可验证且 `errors` 为空；
Publishability 还要求 `source == "background_keyframe_observation"`。Rolling
semantic snapshot 只从满足 Publishability 的工具结果发布；failure、partial、
harness explanatory results，以及 query-time
`realtime_video_memory_unavailable` 说明性结果都只更新失败/可解释状态，不能成为
`current_state`。
When a later chat turn has active video, AgentRuntime may expose the unified
`vision_understanding` tool; ordinary image and explicit-video flows use the same
ToolSpec. The main LLM sees
the live-camera availability, tool schema, and projected
`realtime_video_context`, not frame bytes, frame paths, VLM role prompt, or
Provider payloads. The entry adapter does not call the video provider directly.

The runtime owns a bounded semantic snapshot per opaque `video_id`. Immediately
before every Agent-Service model context build it projects the latest snapshot
into the independent `realtime_video_context` section. Thus the first DeepSeek
decision can use completed Qwen observations or call dynamically exposed
`vision_understanding` when it needs current visual facts. For Agent-Service,
the internal video branch consumes only rolling semantic memory produced
by the background observer; if no semantic text is available yet, the tool
returns a prompt-safe descriptive observation, including
`pending` / `failed` / `unavailable` state, that the LLM can directly use to
explain the situation instead of calling the video Provider with raw frames. Frame
freshness uses capture age; snapshot publication age remains a separate
diagnostic. Ordinary non-Agent-Service video/API requests use the same
`vision_understanding` tool and retain `recent_frame_fallback` behavior.
本地 raw window 仍为 3 帧，语义记忆仍最多保留 8 个成功关键帧；它们不等于 Qwen
单轮输入历史。每个 `video_id` 只持有一个 persistent Qwen WebSocket，observer
concurrency 为一个 in-flight 加一个 latest pending。连接每 20 次成功观察或 60 秒轮换，
断线按有界退避重连；成功、失败、刷新中和陈旧状态都保留最后成功快照。切换 video id、
WebSocket 断开或 observer close 会关闭 Provider session，并清理 pending、语义状态、
retained/raw JPEG 与运行时临时文件。

Vendor chat execution is detached from the WebSocket receive loop, so a long
Gateway turn does not prevent later raw media frames from being validated and
acknowledged. Optional `clientCapabilities` negotiate prompt-free
`chatProgress` and application-level `chatResponseAck`. Media `chat.stream`
independently selects delta delivery; clients that send `stream=false` or omit
it retain single-final-response behavior. Provider-token streaming is enabled
for `stream=true`, while the runtime commit barrier continues to suppress
tool-call preambles. If the media WebSocket disconnects while a detached chat
turn is active, the entry cleanup cancels that task and the facade sends
`run.cancel` with `source=gateway_disconnect` and
`reason=client_disconnected` for the current Gateway run. Abnormal WebSocket
close codes are logged at ERROR with only prompt-safe counters, code, digest and
reason-presence metadata; normal close remains INFO. Per-packet receive/send
logs are DEBUG. Turn latency, ACK, and ordinary protocol failures remain
INFO/WARNING, with one close INFO carrying message/video/byte/failure counters.

## Gateway Responsibilities

Gateway owns the protocol and lifecycle boundary for realtime or Gateway-normalized traffic:

- Accept normalized frames such as `message.user`, `run.cancel`, `ping`, `call.incoming`, `call.hangup`, and `config.update`.
- Validate Gateway-level modality support before dispatching to the assistant backend.
- Bind or preserve `user_id`, `session_id`, `turn_id`, and `run_id`.
- Generate missing Gateway-owned `turn_id` and `run_id` values as typed UUIDv7
  identifiers. Caller-supplied and historical identifiers remain opaque strings;
  Gateway must preserve accepted values instead of parsing or rewriting them.
- Maintain per-session user text history for Gateway turns.
- Register active runs and emit `run.started`, user-visible `event.progress`, `stream.chunk`, and `run.end`.
- Include the assistant backend `trace_id` in `run.end.payload.trace_id` when available so developer/debug entry layers can load trace summaries without exposing raw provider payloads.
- For cancelled turns, include prompt-safe cancel metadata in `run.end.payload.cancel` (`source`, optional `reason`, `phase`, `best_effort`, and `deadline_ms` when applicable). If Gateway ends the turn before a backend trace is available, include `run.end.payload.trace.status=not_available` with reason `cancelled_before_backend_result` instead of inventing a trace id.
- Convert realtime backend events into Gateway wire frames.
- Convert backend failures into protocol-level `run.end` or `error` frames.
- Do not promote an individual handled `tool.failed` event into a Gateway run failure. Tool execution outcomes are
  governed by the generic assistant-loop recovery state machine; when the LLM consumes the failure observation and
  returns a final answer, Gateway emits the ordinary completed lifecycle while trace/response diagnostics retain the
  degraded tool result. Gateway reports failure only when the backend's final run status is actually failed.
- Queue ordinary same-session user messages behind the active run; apply per-session and process-wide limits; cancel either queued or active runs on explicit `run.cancel`, disconnect, deadline expiry, explicit same-session interrupt, or queue-wait expiry.
- On `call.hangup`, cancel queued/active work, stop outbound relay, return `call.hangup_ack(payload.session_closed=true)`, and immediately destroy the logical AgentSession. The same transport may later send a new `call.incoming` and receive a fresh session endpoint.
- Treat cancel/interrupt as a first-class realtime turn outcome. After cancel, old run output is not speakable or user-visible; late backend/tool results may be retained only as trace or stale artifacts.
- Manage per-user logical AgentSession creation/destruction, transport reconnect, idle eviction, and live session config.
- Resolve same-user multi-connection competition at the bridge layer: one session-owned relay is the sole consumer of runtime frames, the newest connection lease owns outbound delivery for that user, and a true current-owner disconnect starts the detached grace timer rather than cancelling immediately.
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

### Lifecycle terminology

The following terms name different state transitions and must not be used as
interchangeable forms of "interrupt":

- **turn arbitration** decides whether a newly accepted utterance is a followup,
  no-op, cancel-only control, revision, or replacement of the active turn;
- **cancel request** targets one queued or active Gateway `run_id` and immediately
  closes the old run's user-visible output gate;
- **cancel checkpoint** is an AgentRuntime implementation boundary where
  cooperative cancellation actually stops execution, commonly before or after a
  provider/tool operation; "在下一个 tool 后插话" describes this checkpoint, not
  a separate Gateway interrupt mode;
- **connection supersede** transfers the per-user delivery lease to a newer
  connection without cancelling the active run;
- **transport disconnect** means the current delivery owner is gone; normalized
  Gateway delivery enters `DETACHED` until reconnect grace expires;
- **session resume** reuses process-local session state and replays retained
  outbox frames after the caller's delivery cursor; it never revives a run that
  already reached `CANCELLED`.

Gateway owns arbitration application, run cancellation intent, stale-output
gating, and connection ownership. AgentRuntime owns the safe checkpoint at which
the cancellation request takes effect. A tool/provider may therefore finish
after cancellation was requested, but its late result is trace-only unless the
runtime contract explicitly marks a reusable artifact; it cannot reopen the old
run's display or speech stream.

### Connection ownership and outbound relay

`GatewayBridge` maintains one process-local connection lease per `user_id` and
one session-owned outbound relay per managed runtime endpoint:

```text
GatewaySession endpoint -> single SessionRelay -> current ConnectionLease sink
                                               -> no owner: bounded outbox
```

The relay, rather than individual WebSocket bridges, is the sole reader of the
session endpoint. Replacing a connection only swaps the current sink and carries
forward the relay's active `session_id` / `run_id` correlation. The old bridge is
stopped as `superseded` and must not emit disconnect cancellation. This avoids
multiple connections racing to consume one endpoint and removes the former need
to re-inject a frame already consumed by a stale bridge.

The current owner's actual transport close is different from supersede: it starts
the configured detach grace period (default 15 seconds). Every externally
forwardable runtime frame receives a monotonically increasing top-level
`delivery_cursor` and is retained in a bounded process-local outbox (default 256
frames). `session.resume` accepts `payload.cursor`; retained frames after that
cursor are replayed before `session.attached`. Its payload reports the latest and
earliest available cursors plus `replay_truncated`, so clients can detect a cursor
older than the retained window. This is bounded reconnect continuity, not durable
cross-process delivery.

If no new owner attaches before grace expiry and a run is still active, the bridge
sends `run.cancel(source=gateway_disconnect, reason=reconnect_grace_expired)`.
A run that completed during detachment is not cancelled merely because its
session id remains known. `call.hangup` remains an explicit immediate end signal
and does not use reconnect grace: it removes the manager entry and closes the
session service/endpoints before a later frame can acquire a fresh AgentSession.
Destroying that logical session does not close the application-owned
`GatewayRuntimePool` or its reusable `AgentGraphRuntime` objects.

These normalized-Gateway semantics do not yet apply across vendor
`/agent-service/v1` WebSocket connections. That adapter still allocates a fresh
internal Gateway session per connection and cancels its connection-owned chat on
disconnect. Cross-connection Media-Agent resume requires a stable authenticated
resume identity and cursor field in the vendor protocol before it can safely use
the shared relay.

## Queue and Admission Contract

Gateway QueuePolicy v1 separates two constraints:

```text
message.user -> per-session FIFO -> process-wide admission -> backend run
                 one head only       bounded/fair FIFO        active permit
```

- `message.user.payload.mode` explicitly accepts `followup|replace`. `followup`
  is the default and queues behind the active turn; `replace` cancels the active
  turn, moves the replacement to the session head, and starts it only after the
  cancelled backend exits. The legacy `interrupt`, `control=interrupt|barge_in|
  cancel_previous`, and queue `mode=interrupt` forms remain compatibility inputs
  only when explicit turn mode is absent. An explicit `followup` is authoritative
  and does not enter semantic arbitration.
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
- The default Gateway backend uses a bounded application-owned runtime instance
  pool. `max_active_runs` remains the admission limit; `max_runtime_instances`
  bounds how many `AgentGraphRuntime` instances the default backend may create.
  The default runtime limit follows `max_active_runs`, and a smaller value is
  rejected at startup. Each admitted turn checks out a runtime and returns it
  after completion/cancellation; connection hangup destroys only its logical
  AgentSession and never closes this process-owned pool. Application shutdown
  closes every pooled runtime and drains its shared bounded post-response memory
  ingestion queue within the configured memory shutdown bound. Pooled
  runtimes also share the application-owned `LongTermMemoryService` and
  `SessionMemorySnapshotStore`, so a
  Gateway session start can prewarm one long-term-memory snapshot before any
  turn, and every turn can reuse it even when Gateway checks out a different
  runtime instance. `call.incoming`, `session.open`, the Media-Agent
  `assistantControl` handshake, and request/response facades enter this
  initialization boundary; the first runtime turn never performs core recall.
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
| `MULTIMODAL_AGENT_GATEWAY_MAX_RUNTIME_INSTANCES` | `MULTIMODAL_AGENT_GATEWAY_MAX_ACTIVE_RUNS` |
| `MULTIMODAL_AGENT_GATEWAY_MAX_PENDING_PER_SESSION` | `8` |
| `MULTIMODAL_AGENT_GATEWAY_MAX_QUEUED_TURNS` | `64` |
| `MULTIMODAL_AGENT_GATEWAY_QUEUE_WAIT_TIMEOUT_MS` | `120000` |
| `MULTIMODAL_AGENT_GATEWAY_DEDUPE_TTL_S` | `300` |
| `MULTIMODAL_AGENT_GATEWAY_DEDUPE_MAX_ENTRIES_PER_USER` | `1024` |
| `MULTIMODAL_AGENT_GATEWAY_DETACH_GRACE_S` | `15` |
| `MULTIMODAL_AGENT_GATEWAY_OUTBOX_MAX_FRAMES` | `256` |

All Gateway queue, dedupe, admission, and default runtime-pool state is process-local
and in memory. Custom backend factories own their own runtime concurrency and are not
wrapped by the default runtime pool. Gateway does not provide restart recovery,
cross-worker consistency, durable-task storage, message collection/summarization, or
live prompt steering. The retained
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
- `POST /tasks/{task_id}/input`
- `POST /tasks/{task_id}/cancel`

The API derives identity from the authenticated context (or explicit local/offline query identity), never from write-body `user_id`. Public projections omit lease tokens, internal input digests and step idempotency keys. FastAPI lifespan reuses the runtime-owned service, optionally starts one cooperative worker, stops it before Gateway shutdown, and closes the SQLite store once.

SQLite tasks survive app restart and expired leases can be reclaimed, but this is not a distributed queue or exactly-once protocol. A step attempt is committed before the external call; expired read-only attempts may retry within budget, while possible writes with uncertain commit state stop at `outcome_unknown` for operator/user resolution. API cancellation also raises a process-local cooperative task token; cross-process cancellation is outside the single-host first-version boundary.

Durable tasks may checkpoint into `waiting_schedule` with a structured UTC
`next_eligible_at`, reason code, bounded summary and optional expiry. SQLite
persists the due time separately from the task JSON so workers do not claim,
lease or consume model/tool budget for an early task. Once due, the service
atomically clears the wait, restores the waiting step to `ready`, records
`task.wake_received` / `task.resumed`, and admits one new bounded quantum.
Cancelled tasks never resume; an expired scheduled wait fails with
`durable_wait_expired`. This is worker scheduling, not a long-lived Gateway run,
WebSocket or coroutine.

Durable tasks may also checkpoint into `waiting_external_event`. That wait
contains an opaque wait id, one `wake_rule_id`, a bounded reason and an optional
expiry; it is never eligible for ordinary worker claim. ProactiveWake continues
to run only governed read probes and, after meaningful evidence changes, emits
a `TaskResumeRequest` rather than mutating the task. `DurableTaskService`
validates authenticated owner, current task version, wait id, rule id and
expiry, then records the evidence fingerprint and requeues the step exactly
once. Stale, duplicate, cross-owner, cancelled and expired requests fail closed.
This protocol keeps event detection outside Gateway and task transition
authority inside the durable service.

`TaskRecord.execution_profile` is a persisted, explicit runtime fact rather
than inferred user intent. The default `agent` profile continues to use
`AgentGraphRuntime`; a `DurableTaskRuntimeRouter` may select a registered,
deterministic workflow for another exact profile. The first such workflow is
`hotel_price_watch_v1`: each bounded quantum validates and executes the
read-only `lodging_search` Tool through
`ActionValidator -> ToolExecutor -> ToolRegistry`, compares the structured
nightly price with the persisted goal, then either schedules the next check,
completes without action at expiry, or completes with one idempotent
notification request. It has no booking, inventory-hold or payment operation.
Provider failure becomes a bounded scheduled retry and remains visible in
workflow state; cancellation and the task deadline still belong to the common
durable state machine.

The local runtime uses separate bounded budgets for LLM-backed `agent` quanta
and deterministic workflow quanta. Hotel watch creation is exposed only as
`hotel_price_watch_create` in explicit durable/plan mode; foreground mode
rejects all durable task submission tools. Local deployment controls are:

```text
MULTIMODAL_AGENT_DURABLE_TASKS_ENABLED
MULTIMODAL_AGENT_DURABLE_TASK_WORKER_ENABLED
MULTIMODAL_AGENT_DURABLE_TASK_PATH
MULTIMODAL_AGENT_DURABLE_TASK_MAX_SECONDS
MULTIMODAL_AGENT_DURABLE_WORKFLOW_MAX_QUANTA
MULTIMODAL_AGENT_DURABLE_NOTIFICATION_PATH
MULTIMODAL_AGENT_DURABLE_NOTIFICATION_WORKER_ENABLED
```

The built-in notification delivery worker starts only when explicitly enabled
and only uses the mock transport in mock mode. Real mode may persist the
outbox, but no channel sender is silently substituted.

## Realtime Semantic Interrupt Arbitration

Gateway supports two interrupt models at the lifecycle layer:

- `explicit_control`: a user button, entry-adapter control, `interrupt=true`,
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

No current built-in entry declares `supports_semantic_interrupt=true`, and an
explicit `message.user.payload.mode` always bypasses semantic arbitration.
`/agent-service/v1` uses the vendor's explicit `interrupt` message and never waits
for semantic arbitration. The generic machinery remains capability-gated for a
future trusted entry and is not a second media protocol.

The control-plane flow is:

```text
ordinary realtime transcript while run R1 is active
    |
    |-- accepted as queued turn R2; no backend permit consumed
    |-- the entry adapter may already pause or duck TTS
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

The media-facing entry owns immediate audio experience. It may pause or duck TTS
when the user speaks and later resume or supersede output based on the decision.
The Python Gateway owns text/run visibility after cancellation but does not
pretend to control a TTS provider that is outside this repository.

The process policy is configured with strict values:

| environment variable | default |
| --- | ---: |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_ENABLED` | `false` |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_TIMEOUT_MS` | `1000` |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MAX_CONCURRENCY` | `2` |
| `MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MIN_CONFIDENCE` | `0.80` |

Mock mode uses a deterministic `UNCERTAIN` fallback. An actual LLM arbitration
call requires `MULTIMODAL_AGENT_PROVIDER_MODE=real` and a non-mock, configured
chat adapter; the presence of an API key alone does
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

Entry adapters may be implemented in TypeScript, Go, Rust, or another language when that better fits a Web UI, BFF, vendor WebSocket adapter, edge deployment, or telephony/media SDK. Those non-Python layers should stay thin: parse product or transport payloads, enforce entry-layer auth and UX contracts, and forward normalized HTTP requests or Gateway frames to the Python `assistant_agent` Gateway/runtime boundary without reimplementing assistant loop, Gateway lifecycle, tool calling, memory, or provider policy.

## Entry Identity and Session Rules

Gateway entry adapters must bind identity before a user turn reaches the assistant backend:

- HTTP `/agent/run` resolves authenticated request identity at the route boundary, then runs the turn through `GatewayTurnFacade` with the resolved `user_id` and `session_id`.
- Gateway WebSocket `/ws/gateway` resolves the WebSocket identity from auth/query context, rejects mismatched frame `user_id` or `session_id`, and injects trusted `source=gateway_websocket` metadata only after the frame passes that check.
- Vendor `/agent-service/v1` preserves the vendor envelope at the entry layer, but `chat` turns use a local `GatewayTurnFacade`. Raw `audio` remains entry-layer ACK traffic; `interrupt` cancels the active turn through Gateway, cancels locally queued connection-owned chat turns, and suppresses stale output before returning its ACK. Raw self-contained H.264 `video` is validated and decoded at the entry boundary into a bounded local frame context; only its stable `video_id` is promoted on a later Gateway chat turn.

Entry adapters may attach prompt-safe `entry_capabilities` metadata so downstream code can distinguish text streaming, interrupt support, TTS state support, realtime task-state support, media reference support, raw media support, TTS edge event support, and App shopping-detail presentation without inferring behavior from transport names. These capability declarations are informational; they do not authorize tool calls, provider selection, memory access, or new modalities.

`supports_shopping_detail_v1=true` 只表示客户端能够渲染 App shopping card protocol，不授权入口层
重写回答。`shopping_search` 的结构化 observation 携带展示模板，下一轮 LLM 可在最终文本中生成唯一
`<detail>...</detail>` 块。Realtime 将 Provider/Runtime 最终文本按普通 response delta/final 语义原样
交付；conversation history、`AgentResponse.message`、Gateway result 与客户端看到的正文保持一致。

Realtime task-state is opt-in at the request/capability level. Ordinary Gateway metadata, `source=gateway_*`, or a `realtime.run_id`/`turn_id` pair does not by itself enable phone/realtime task semantics. `/agent-service/v1` declares `supports_realtime_task_state=true`; ordinary request/response chat facades should leave that capability false unless they explicitly want realtime interruption, pending-tool, TTS/display, and artifact-reuse behavior.

## Hermes-Inspired Boundaries

Hermes' message gateway is useful reference material for defensive edge handling, but this project does not adopt its multi-IM runtime shape. Borrowed ideas should stay within the current Gateway boundaries:

- Session isolation maps to explicit `user_id`, `session_id`, trusted `source`, and session config handling, not platform-specific session-key factories.
- Running-agent control interception maps to Gateway control frames: `run.cancel`, explicit interrupt metadata, deadline cancellation, disconnect cancellation, and `call.hangup`.
- Adapter capability fallback maps to small entry capability declarations and outbound formatters, not platform-specific send APIs in the Gateway core.
- Hook-style lifecycle visibility maps to controlled Gateway lifecycle events and trace/observability records, not arbitrary user hook execution in the Gateway process.
- Platform formatting, slash commands, memory flush, and external delivery routing remain outside Gateway unless implemented as thin entry adapters that forward normalized Gateway frames.

## Media-Agent WebSocket

`/agent-service/v1` is the only Media Service WebSocket entry. Its external wire
contract, H.264 constraints, streaming response format, ACK rules and examples
live in `docs/media-agent-service-websocket.md`.

The entry adapter validates the vendor envelope, decodes bounded self-contained
H.264 frames into runtime-owned video context, and maps `chat` into a local
`GatewayTurnFacade`. The facade and `GatewaySessionManager` own run identifiers,
history, cancellation and terminal lifecycle. A vendor `interrupt` cancels the
active Gateway run and locally queued chat turns before its ACK is returned;
stale output is not delivered. WebSocket disconnect closes the same owned
resources and cancels an active run.

Local Media-Agent testing uses `scripts/run_client.py`. A future Web UI or edge
adapter may use `/ws/gateway` with normalized frames, but it must not introduce a
second media wire protocol or a second assistant runtime.

System prompt profile selection remains trusted session configuration.
Agent-Service uses `system_prompt_profile=text_default` and
`channel=realtime_phone`; channel metadata does not alter the AgentRuntime
identity or system prompt. User payload metadata cannot promote a normal turn
into another profile.

TTS remains an entry-adapter concern.
`assistant_agent.media.audio_edge.gateway_frame_to_tts_event()` can map
speakable Gateway frames into prompt-safe TTS edge events. It does not invoke a
TTS provider, stream audio, or change assistant runtime behavior.

## Runtime Adapter Contract

Gateway talks to assistant execution through modules owned by `assistant_agent.gateway`:

- `RealtimeAgentRequest`: normalized user turn payload from Gateway.
- `RealtimeAgentEvent`: assistant-side stream events that can be mapped to Gateway frames.
- `RealtimeAgentResult`: terminal backend status, response metadata, trace/run IDs, and `expects_reply`.
- `RealtimeAgentBackend`: backend protocol implemented by `GatewayRuntimeAdapter`.
- `RealtimeCancelToken`: cooperative cancellation token passed from Gateway to the backend.

`GatewayRuntimeAdapter` is the single thin adapter implementation. Runtime request/event/result types remain realtime-oriented protocol concepts, but they do not form an independent package or execution layer.

Long-running assistant turns can emit `RealtimeAgentEvent(type="run.progress", display_only=True)` for user-visible status updates such as current work, completed step, next step, blocked state, or needed user decision. The realtime adapter applies progress throttling and idle heartbeat policy before Gateway maps those updates to `event.progress` frames; entry layers decide how to display them and should not treat them as final answer content.

FastAPI 默认 Gateway manager 与 Agent-Service 每连接 local manager 都绑定同一个
prompt-safe lifecycle logging sink。该投影覆盖 session、queue、admission、run、
cancel/interrupt 和 terminal 事件，保留 `run_id` / `turn_id`，并将 `user_id`、
`session_id` 转为稳定短摘要；它不记录用户文本。sink 继续遵守 Gateway 既有
fail-open 约束，日志配置或写入失败不得改变 frame、排队、取消或 terminal 行为。
可读日志写入 `.data/logs/gateway.log`，跨 runtime 的 trace 语义与安全 allowlist
仍以 `docs/observability-harness.md` 为权威。

Realtime event projection carries stable delivery semantics without moving control flow out of the assistant loop:

| runtime event | Gateway frame | speech policy | persistence | replacement behavior |
| --- | --- | --- | --- | --- |
| `response.chunk` | `stream.chunk` | `required` | `final` | supersedes the run progress slot |
| `run.progress` | `event.progress` | `optional` | `ephemeral` | replaceable at `<run_id>:progress` |
| `tool.started` / `tool.finished` / `tool.failed` | `event.tool` | `never` | `ephemeral` | not replaceable |

Every `run.end` supersedes the same progress slot, including completed, failed, and cancelled runs. This lets an entry adapter remove already displayed progress after a final answer or cancellation. Progress and tool lifecycle events remain display/trace state; they are not assistant final text and must not be promoted into conversation history or long-term memory.

Provider text remains provisional until the current native LLM call is known not to contain tool calls. The runtime buffers streamed text for every tool-capable iteration, discards it when that iteration returns tool calls, and flushes it only when the iteration resolves as a final answer. This commit barrier prevents tool-call preambles from becoming `stream.chunk` output while keeping streaming as an event projection rather than the assistant loop itself.

The repository still does not invoke a TTS provider. `speech_policy`, `persistence`, `replaceable`, `replacement_key`, and `supersedes` are prompt-safe entry-layer facts that UI or future audio adapters can consume.

This boundary lets Gateway preserve OpenClaw-compatible session/run semantics without making Gateway depend on `AgentGraphRuntime` internals, `AgentRouter` internals, worker agent contracts, or a legacy OpenClaw adapter. If multi-agent realtime behavior is needed, the realtime turn must enter the main `AgentGraphRuntime` / assistant loop first; that main runtime can then delegate through the tool-governed agent communication boundary. Do not teach worker agents Gateway frames such as `call.incoming`, `call.hangup`, or WebSocket payloads.

## Current Code Map

| module | responsibility |
| --- | --- |
| `src/assistant_agent/gateway/protocol.py` | Gateway wire frame helpers, call/config constants, and supported modalities. |
| `src/assistant_agent/gateway/capabilities.py` | Prompt-safe entry adapter capability declarations used in Gateway metadata. |
| `src/assistant_agent/gateway/observability.py` | Controlled fail-open Gateway lifecycle event model and sink helper. |
| `src/assistant_agent/gateway/queueing.py` | Bounded queue policy, process-local fair run admission, stable queued-turn records with nested semantic-arbitration state, and TTL/LRU retry identity index. |
| `src/assistant_agent/gateway/transport.py` | Transport-agnostic endpoint primitives for in-process tests and embedding. |
| `src/assistant_agent/gateway/ws.py` | JSON text WebSocket adapter that presents a WebSocket as a Gateway endpoint. |
| `src/assistant_agent/gateway/bridge.py` | External-client-to-session bridge: call lifecycle, one session-owned bounded/cursor outbox relay, per-user connection leases, detached grace, replay, disconnect-expiry cancellation, and modality gate. |
| `src/assistant_agent/gateway/session.py` | Gateway-managed session service: `message.user`, `run.cancel`, session history, active runs, interrupt, deadline, event mapping, and session manager. |
| `src/assistant_agent/gateway/runtime_backend.py` | Gateway-owned backend protocol and event sink contract. |
| `src/assistant_agent/gateway/runtime_types.py` | Gateway-to-Runtime request, event, result, capability, and cancellation types. |
| `src/assistant_agent/gateway/runtime_adapter.py` | Thin adapter from Gateway turns to the shared assistant Runtime. |
| `src/assistant_agent/gateway/runtime_event_mapping.py` | Runtime `AgentEvent` to Gateway-owned runtime event mapping. |
| `src/assistant_agent/gateway/event_mapping.py` | Gateway-owned runtime event to wire-frame mapping. |
| `src/assistant_agent/gateway/delivery.py` | Display, persistence, replacement, and supersession policy. |
| `src/assistant_agent/gateway/progress.py` | Progress throttling and heartbeat projection. |
| `src/assistant_agent/gateway/ws_server.py` | Optional standalone Gateway session WebSocket server entrypoint, not the main FastAPI app route. |
| `src/assistant_agent/media/audio_edge.py` | Prompt-safe helper for entry adapters that convert speakable Gateway text frames into TTS edge events without invoking a provider. |
| `src/assistant_agent/api/gateway_runtime.py` | Process-local FastAPI-owned `GatewaySessionManager`, `GatewayBridge`, `GatewayTurnFacade`, HTTP response capture, and shutdown cleanup. |
| `src/assistant_agent/api/gateway_websocket.py` | FastAPI entry adapter for normalized `/ws/gateway` frames. |
| `src/assistant_agent/api/agent_service_websocket.py` | FastAPI compatibility adapter for the vendor `/agent-service/v1` media protocol; preserves `message` / optional `sessionId` / stringified `body` envelopes, accepts media `assistantControl` / `chat` / `audio` / `video` / `interrupt`, ingests self-contained H.264 video frames, and routes chat plus stable video references through a local `GatewayTurnFacade`. |
| `src/assistant_agent/media/video/h264_video_ingestion.py` | Entry-layer H.264 validation, bounded FFmpeg I-frame decode, JPEG artifact lifecycle, and registration in the runtime-owned `VideoContextStore`; never calls an understanding provider. |
| `src/assistant_agent/media/video/realtime_video_observer.py` | Per-connection local adaptive selection, retained-keyframe lifecycle, latest-wins background scheduling, and governed `vision_understanding` video-branch execution. |
| `src/assistant_agent/media/video/realtime_video_memory.py` | Runtime-owned bounded prompt-safe semantic video snapshots, health/failure state, and per-video isolation. |
| `src/assistant_agent/api/` | FastAPI HTTP/WebSocket entry adapters and product API routes. |
| `src/assistant_agent/gateway/turn_facade.py` | In-process sync-turn facade for request/response entries that need Gateway lifecycle semantics without a WebSocket transport. |
| `src/assistant_agent/observability/operational_logging.py` | Central prompt-safe console/file logging setup, Gateway lifecycle projection, identifier digesting, and write-only runtime trace projection. |
| `src/assistant_agent/runtime/assistant_runtime_app.py` | Backend-to-runtime boundary used behind `GatewayRuntimeAdapter`; owns the internal runtime reference without becoming the target product entry boundary. |
| `src/assistant_agent/runtime/assistant_run_service.py` | Shared assistant request/run service used behind `AssistantRuntimeApp`, plus eval and demo utilities. |
| `scripts/run_demo_flows.py` | Offline demo/scenario entry adapter that runs scenarios through a local `GatewayTurnFacade` and formats the existing demo summary payload. |
| `scripts/run_client.py` | Local Media-Agent protocol console client for `/agent-service/v1`; supports repeated `chat` sends and `/new [sessionId]`. |

### Entry Convergence Inventory

Phase 0 treats these entry classifications as architecture contracts:

| Entry | Current path | Classification |
| --- | --- | --- |
| HTTP `POST /agent/run` | `routes_agent.run_agent -> _run_agent_through_gateway -> GatewayTurnFacade -> GatewaySessionManager -> GatewayRuntimeAdapter -> AssistantRuntimeApp -> run_assistant_request -> AgentGraphRuntime` | Canonical Gateway-first product entry. |
| Gateway WS `/ws/gateway` | `gateway_websocket -> get_gateway_bridge().bridge(...) -> GatewaySessionManager` | Canonical normalized Gateway entry. |
| Local CLI `--text` | local `GatewaySessionManager + GatewayTurnFacade + GatewayRuntimeAdapter` | Canonical local Gateway-first entry. |
| CLI `--scenario` | demo matrix through local `GatewayTurnFacade` in `scripts/run_demo_flows.py` | Offline demo adapter, Gateway-first internally. Do not expand into product behavior. |
| Vendor `/agent-service/v1` | vendor `message` / optional `sessionId` / stringified `body` protocol; `assistantControl` is the media handshake, legacy `assistantControlStart` remains accepted, raw H.264 I-frames become a bounded local JPEG context, and `chat` uses local `GatewayTurnFacade` with the stable session video reference | Compatibility vendor surface, Gateway-first internally for chat and video references. H.264 decode stays at the entry boundary; tool choice and provider calls stay in the assistant runtime. |
| HTTP `POST /agents/run` | explicit `AgentRouter` service call | Separate opt-in router/debug entry, not the default product path. |
| Inbound A2A `/a2a/rpc` | protocol adapter over `AgentRouter` | Explicit adapter, not Gateway lifecycle. |
| MCP `tool_run` | `ActionValidator -> ToolExecutor -> ToolRegistry` | Tool adapter path, not assistant entry. |
| Removed legacy Web Chat | `/demo/console`, `/static/index.html`, and `/ws/agent/{session_id}` are not registered or shipped | Ordinary browser chat is out of scope until the realtime assistant runtime is stable. |

The default pytest safety net protects cancellation termination, the core realtime-event to Gateway-frame
conversion contract, Media-Agent explicit interrupt behavior, explicit followup/replace behavior, same-user
connection takeover without false disconnect cancellation, and cursor replay within detached grace. Add more
pytest only for a concrete regression or changed stable protocol contract.

## OpenClaw Reference Boundary

Use `/home/lenovo1/pycharm_project/runTime` only as a reference for compatibility behavior:

- Frame names and payload semantics: `message.user`, `run.started`, `stream.chunk`, `run.end`, `run.cancel`, `ping`/`pong`, call frames, and config frames.
- Session lifecycle: active run registration, per-session history, generated IDs, reconnect, immediate logical-session destruction on hangup, idle eviction, and terminal `expects_reply`.
- Cancellation and interrupt behavior.
- Transport adapter behavior and Gateway WebSocket bridging.

Do not import `openclaw_gateway_runtime`, reuse the old OpenClaw/Anthropic agent loop, or make OpenClaw adapter selection part of the current assistant runtime. If OpenClaw behavior conflicts with this document or `AGENTS.md`, this project's current architecture wins unless the user explicitly asks for a compatibility change.

## Update Rules

- Keep current Gateway protocol, lifecycle, adapter, and entry-layer decisions in this file.
- Keep `AGENTS.md` as the concise routing entry and this file as the Gateway-specific authority.
- Keep `.codex/skills/assistant-runtime-reference/SKILL.md` routing to this file before any legacy `runTime` reference.
- Do not put active Gateway architecture decisions only in `docs/development/**`; retained development files are runbooks or explicitly named execution material.

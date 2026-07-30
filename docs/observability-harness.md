# Observability Harness

Last updated: 2026-07-29

This document is the current entry for assistant run status, logs, monitoring,
trace, and ReAct checkpoint observability. It defines the developer-facing
harness contract for understanding one run end to end. Trace content capture is
enabled by default and preserves evaluation evidence including requests,
responses, model messages, tool observations, and memory operations. Credentials,
authorization material, hidden reasoning, and inline binary media remain excluded.

## Agent-Service Delivery Audit

`/agent-service/v1` writes prompt-safe delivery transitions to
`.data/agent_service_delivery.jsonl`. Records distinguish `accepted`,
`processing`, `sent`, `acked`, `failed`, `disconnected_before_send`, and
`disconnected_before_ack`. A `sent` record only proves that WebSocket
`send_text()` returned; only `acked` proves the media application processed the
final response.

A streamed failure terminal (`code=FAIL`, `final=true`) closes the Media stream
but has no `deliveryId` and is not ACK-negotiated. Only a successful terminal
delivery can transition to `acked`; a failure terminal remains `failed` and can
never produce `agent_service.delivery.acked`, even when its terminal packet was
successfully handed to the WebSocket.

Records use digests for session and chat identifiers and may include run/trace
ids, close code, and a close-reason category. They never include response text,
raw media, phone numbers, credentials, or provider payloads. This JSONL file is
local runtime evidence, not a durable cross-host delivery database.

## Agent-Service Turn Latency

Every accepted `/agent-service/v1` chat delivery can produce one prompt-safe
`agent_service.turn.finished` summary after the final WebSocket `send_text()`
returns. This is the user-visible send boundary. When `chatResponseAck` was
negotiated, application delivery confirmation remains a later, separate
`agent_service.delivery.acked` event. A turn with `ack_status=pending` was sent
successfully but has not been confirmed by the media application. Failure
terminals are never `ack_status=pending` and carry no ACK-able delivery ID.
The terminal event carries the same top-level `user_id` and internal
`agent-service-*` `session_id` as the Assistant runtime events, so machine-log
filters and `last --follow` do not lose the accepted chat's session identity.
Its bounded attributes also include `client_type`: omitted media handshakes are
classified as `media_agent`, while the local `scripts/run_client.py` console is
classified as `run_client`.

关联标识保持最小且职责明确：

- `delivery_id` identifies media delivery and optional ACK state;
- `run_id` identifies one execution from Gateway ingress through Assistant Runtime
  and delivery audit;
- `trace_id` is the common lookup key used by trace queries and `agentruntime_view.py`.

The runtime preserves the ingress `run_id` and publishes `trace_id` with its first
`task_started` progress fact, so an entry deadline does not lose an already-created
partial trace. Timeout summaries deliberately separate `entry_status=failed`,
`runtime_status=pending_cancel`, and `terminal_status=unknown`. The later raw
`run.cancelled` or `run.failed` event remains the runtime terminal truth; observers
must not fabricate matching `*.finished` spans for work that was still exiting.
`agent_service_turn_latency_v2` records the safe failure code/source, deadline,
and latest unmatched started span as `active_stage`.

The latency summary is a non-overlapping critical-path view where possible:

| level | stages | interpretation |
| --- | --- | --- |
| Media transport | `entry_parse`, `chat_queue_wait` | Request validation and same-session serialization before Gateway execution. |
| Assistant leaves | `conversation_prepare`, `memory_load`, `context_build`, `llm_chat[n]`, `action_validation`, `tool_execute[name]`, `response_finalize`, `runtime_postprocess` | Work performed by the Assistant runtime. |
| Gateway/response | `gateway_overhead`, `websocket_send` | Gateway wrapper cost not explained by backend execution, then final socket backpressure. |
| Residual | `unattributed` | Positive end-to-end time not represented by the measured leaves. |

For LLM stages, wall time participates in the critical path; Provider-reported
latency is a nested diagnostic and is not added again. The bottleneck is the
largest critical-path stage, including positive `unattributed`. ACK latency and
background video diagnostics are secondary measurements and do not change the
send-path bottleneck.

Tool stages follow the same rule: `tool.finished` / `tool.failed` 的顶层
`latency_ms` 必须覆盖 `tool.started` 到 terminal commit 的 executor wall time。
工具自身返回的 `ToolResult.latency_ms` 只作为
`tool_reported_latency_ms` 嵌套诊断，不能替代 wall time，否则 Provider
轮询、adapter 包装或 commit 前等待会被错误计入 `unattributed`。

The foreground assistant loop must emit one paired `llm.chat.started` /
`llm.chat.finished` span for every Provider attempt. The finished event supplies
`wall_latency_ms` and `provider_latency_ms`, allowing the turn summary to name
`llm_chat[n]` instead of folding model time into `unattributed`. 默认安全模式下
`llm.chat` generation 不设置 `langfuse.observation.output`；Provider/model、finish
metadata、usage 和 latency 使用独立 generation attributes。
`assistant_agent.result_kind` 是观测层根据归一化 `ChatResult` 即时计算的
`error | tool_call | refusal | truncated | text | empty`，不属于 Qwen/OpenAI 协议，也不写回
`ChatResult`。本地 Langfuse 将 `llm.chat` generation 固定为两个按顺序展示的
字段：input 是 Provider adapter 传给 SDK 的完整调用参数，output 是保留 `role`、`content`、
`tool_calls` 和可选 `refusal/errors` 的 OpenAI-compatible assistant message。
Langfuse Formatted 面板可能折叠长字符串；是否完整以 observation input JSON/Public API 为准。
system message 把可信运行时间放在第一段，使折叠预览也优先显示日期事实。
运行时分支和传输模式继续作为 observation metadata，不再包进 output preview。
每个 attempt 另外记录 `attempt_kind`（当前包括 `primary`、`context_overflow_retry`、`finalize` 和
`finalize_protocol_retry`），避免同一 ReAct iteration 内的上下文溢出或最终回答纠正被误读成
独立行动决策；`run_phase=act | finalize` 直接记录本次请求所处的 Runtime 阶段。
Provider-native 终态按 `tool_calls`、refusal、`finish_reason=length`、content、empty/error
的固定运行时顺序路由，不再产生 JSON contract validation 或 repair span。
归一化后的 `attributes.usage.prompt_tokens/completion_tokens/total_tokens` 会映射为
Langfuse generation 的 `usage_details.input/output/total`，并同步写入 OTel
`gen_ai.usage.input_tokens/output_tokens`；usage 嵌套结构不能被当作普通标量属性而丢弃。

工具调用预算耗尽或 runtime guard 要求停止行动时，Runtime 从 `ACT` 切换到 `FINALIZE`。下一次
Provider request 保留原始用户目标和已经发生的成对 native action trajectory；每个 tool result 仍是
prompt-safe 的结构化 observation，末尾追加无工具续答消息，并显式发送 `tools=[]`、
`tool_choice=none`。若 Provider 仍返回 tool call，Runtime 将其记录为
`finalization protocol violation`，不执行工具，最多进行一次 `finalize_protocol_retry`；再次失败时
从结构化失败事实生成不包含内部限制数值的诚实降级答复。FINALIZE 直接返回 error、truncated 或
empty 时也使用同一降级，而不丢弃已经取得的工具事实。仅供 Runtime 诊断的 guard observation、具体
预算、跳过数量和 guard 原因只保留在 trace/metadata。

`run_phase` 是 phase 控制的唯一事实；`runtime.phase.changed` 明确记录 `from_phase`、`to_phase`、
`reason` 和 `source`。`react.iteration` 继续表示一次模型决策循环，因此 FINALIZE 请求仍位于新的
`react.iteration` 内，其实际阶段以内部 `llm.chat.run_phase` 为准。
`loop_guard.triggered` 是通用 guard 事实，通过
`disposition=block_action | finalize | terminate` 表明阻止当前动作、进入最终回答或直接终止，
不能仅凭事件名推断一定进入哪一个 phase。

read Tool 的 Provider 自动重试发生在同一个 Tool span 和 `tool_call_id` 内，不产生新的
`react.iteration`。每次可重试失败记录 `tool.attempt.failed` 和 `tool.retry.scheduled`，最终
`tool.finished` / `tool.failed` 记录 `attempt_count`、`execution_retry_count` 和
`retry_exhausted`；模型修改参数后发起的新 Tool call 是独立的 ReAct action。
完全相同且已完整成功的 read Tool 再次调用会由 loop guard 在 executor 前阻止，并进入
`FINALIZE`，因此不会产生第二个 `tool.started`。Provider 返回空 tool call ID 时，adapter boundary
先生成唯一 ID，后续 `tool.observation` 与 FINALIZE transcript 使用同一 ID。

The versioned `agent_service_turn_latency_v2` summary also exposes only bounded
stream facts: `stream_requested`, `provider_token_stream_seen`,
`stream_chunk_count`, `first_stream_chunk_latency_ms`, and
`final_response_sent`. The count is incremented only after a provider-token
delta packet is successfully handed to the WebSocket; neither delta text nor
the final answer is retained in this summary. `provider_token_stream_seen` is
set when the Provider delta reaches the entry projection, so a disconnected
send can report `provider_token_stream_seen=true` with `stream_chunk_count=0`.
`final_response_sent=true` means a terminal response was successfully handed to
the WebSocket; it covers both `SUCCESS` and `FAIL` terminals and is not a
business-success flag.

## Assistant Turn Summary

Every terminal Assistant turn writes one prompt-safe `assistant.turn.summary`
trace event with `schema_version=assistant_turn_summary_v2`. This event is the
canonical machine fact for developer-facing turn identity and terminal status.
Raw trace timeline events remain the detailed diagnostic source, but viewers
and local tools should prefer the summary when deciding session banners, client
type, readiness, terminal status, response presence, tool/error counts, and
bounded failure text.

The summary is appended after runtime postprocess for ordinary runtime turns.
Agent-Service suppresses that ordinary runtime write and appends the same schema
after `agent_service.turn.finished`, so the summary can include the Gateway run,
turn id, media session turn, classified client, and a safe reference to the
latency event. This keeps one summary per terminal turn while preserving the
existing `agent_service_turn_latency_v2` schema.

Allowed summary fields are:

- `trace_id`, `run_id`, optional `turn_id`;
- `user_id`, `session_id`, optional `session_turn`;
- `client_type` in `api`, `cli`, `gateway`, `media_agent`, `run_client`, or
  `unknown`;
- `terminal_status` in `completed`, `failed`, `cancelled`, or `unknown`;
- `response_present`, `tool_count`, `error_count`;
- optional bounded `failure_summary`;
- optional `latency_summary_ref` pointing at `agent_service.turn.finished`.

The event must not contain user text, assistant response text, prompts, provider
raw payloads, memory contents, media bytes, or provider/tool request bodies.
Failure summaries are sanitized and bounded; current-turn user/assistant text
for failed or cancelled local debugging remains only in the explicit
`--allow-local-trace-content` trace-conversation overlay.

The safe INFO records have this shape and never contain prompts or responses:

```text
turn_latency status=sent trace=trace_x run=run_x delivery=delivery_x session_turn=2 total=824ms bottleneck=llm_chat[2] bottleneck_ms=410ms share=49.8%
delivery_ack status=acked trace=trace_x run=run_x delivery=delivery_x session_turn=2 ack_latency=18ms
```

`scripts/run_server.py` enables an in-memory primary trace store plus a bounded
background JSONL writer. Response delivery never waits for JSONL I/O: a full
secondary queue drops observability events and increments its drop counter.
Shutdown attempts a bounded flush. This persistence is local diagnostic data,
not a delivery authority.

## Operational Logging

本地 server 在不改变 FastAPI 单进程结构的前提下提供 Gateway operational logging；
Gateway 开发视图通过 `scripts/gateway_view.py last --follow` 读取 Gateway lifecycle JSONL，
Assistant runtime 开发视图通过 `scripts/agentruntime_view.py last --follow` 读取 canonical
trace。控制台是 Combined 摘要，`.data/gateway_events.jsonl` 接收 server 启动 marker
与 Gateway lifecycle，`.data/logs/gateway.log` 只保留兼容的 key=value text projection。
Combined 默认采用 `concise` 模式，只显示关键 Gateway lifecycle
以及普通应用 WARNING/ERROR 的 logger 名；Gateway 与 runtime 开发者 timeline
都不依赖 Assistant Server 控制台分栏。`component=runtime` 的文本日志不进入
Combined，即使 `verbose` 也不作为 runtime 观察入口。未经过安全投影的普通应用
message 不在 Combined 原样显示，即使 `verbose` 也只显示其 logger 元数据。
控制台 INFO/DEBUG 写 stdout，WARNING/ERROR 写
stderr，避免 PyCharm 将所有正常事件渲染为红色；控制台只显示短关联 ID，不显示稳定
身份摘要或密集 payload。Uvicorn 内建 INFO 默认降噪，避免 WebSocket 握手 query value
绕过安全投影进入控制台；只有显式 `--access-log` 时才恢复其 INFO/access 输出。
Gateway JSONL 使用 `gateway_lifecycle_event_v1` schema，并保留完整可用的
`run_id`、`turn_id`、`trace_id`，用于从入口 lifecycle 串联到 runtime trace。
兼容 text log 继续使用 UTC `key=value` 格式，通过标准库 `RotatingFileHandler`
轮转，单文件上限 5 MiB，保留 3 个备份；重复配置或 reload 不得重复安装 handler。
launcher 通过显式进程环境把 JSONL/text path 传给 `create_app()`，因此 reload 后的实际
server 子进程会重新执行同一幂等配置。JSONL 或 text handler 打开失败时保留 Combined
console 并 fail-open，不得阻止应用启动。

Gateway lifecycle sink 在 server 启动时先写一条 `gateway.server.starting` marker，便于
`scripts/gateway_view.py last` 或 `--follow-include-existing` 回看当前入口状态；之后覆盖
session、queue、admission、run、cancel、interrupt 和 terminal 边界。它保留 `run_id` / `turn_id`，
但只记录 allowlist 内的状态、计数、reason/source 等 prompt-safe 字段；`user_id` 与
`session_id` 使用稳定短摘要，不记录用户文本。Agent-Service 连接日志同样只记录
query key、session 摘要和聚合计数，不记录 query value、原始 session ID 或媒体内容。

Assistant runtime 不再投影到 operational text log，也不再创建 `.data/logs/runtime.log`。
server `CompositeTraceStore` 默认只保留进程内 primary 与后台 JSONL persistence；
`.data/graph_trace.jsonl` 和 trace query API 是机器查询与调试重建权威，
`scripts/agentruntime_view.py last --follow` 是唯一 runtime 开发观察视图。显式设置
`ASSISTANT_AGENT_OTEL_EXPORT_ENABLED=true` 且提供 Langfuse 凭据时，server 会追加
一个 optional text OpenTelemetry observer secondary；依赖缺失、endpoint 缺失或导出失败必须
fail-open，不影响 primary trace store、JSONL persistence 或 turn 响应。

`scripts/run_server.py` 提供 `--console-level`、`--file-log-level`、
`--console-mode {concise,verbose}`、`--log-dir PATH` 与 `--gateway-event-path PATH`，
默认分别为 `INFO`、`DEBUG`、`concise`、`.data/logs` 和 `.data/gateway_events.jsonl`；
`--file-log-level` 只控制兼容 `gateway.log`。旧
`--log-level` 仍作为同时覆盖 console/file level 的兼容 shorthand。共享 PyCharm 配置
`.run/Assistant Server.run.xml` 使用 `hello_agent` 解释器并读取本机未跟踪 `.env` 中的
provider mode 和 Provider 配置启动：
Run console 只保留 launcher 输出与 WARNING/ERROR。该配置显式设置 operational logging
环境变量，确保 launcher 与 reload 后的 server 子进程写入同一 Gateway JSONL/text 文件，但不再
声明 PyCharm `log_file` 页签。Gateway 开发观察统一运行 `.run/Gateway.run.xml`，
它执行 `scripts/gateway_view.py last --event-path .data/gateway_events.jsonl --follow`。
runtime 开发观察统一运行 `.run/AgentRuntime.run.xml`，它常驻跟随
`.data/graph_trace.jsonl`，连接本地 8089 server，优先用 Turn summary 识别终态和
session，但不单独渲染 Turn summary 块。Human view 默认先输出 Turn Overview；
Conversation、Decision Trace 和 Raw events 作为显式层级展开。ReAct 决策、
validator 和 tool 证据在 Decision Trace 中按 iteration 聚合，Raw events 保留完整事件线。
`.run/Langfuse.run.xml` 无需参数，通过 `scripts/run_langfuse.py` 启停本机 Compose stack；
停止该 Run 配置只执行 `docker compose stop`，不会删除数据卷。`.run/Assistant Client.run.xml`
同样固化本机 8089 server、stream、progress、ACK 和 interactive 参数，作为文本手工测试入口。
`.run/Mem0.run.xml` 通过 `scripts/run_mem0.py` 启动 Mem0 与 Qdrant；健康检查通过后该 Run
配置正常结束，但两个容器继续在后台运行。

`Assistant Server` launcher 在 Provider 摘要之后一次性输出 `Dependencies`：Mem0 仅在
real mode 且配置 `MEM0_BASE_URL` 时探测，状态为 `disabled / ready / unavailable`；
Langfuse 同时显示服务可达性与 `export enabled/disabled`；Web search 显示随 runtime
进程使用的联网能力 readiness：mock 模式为 `ready (mock)`；real Qwen 模式复用完整 Chat
Provider 配置并显示 `ready / unavailable (bailian native turbo)`。真实 runtime 不再装配
Tavily/HTTP `web_search` 或 `web_fetch` Tool，因此旧搜索 Provider key 不参与启动 readiness。
Mem0 与 Langfuse 的 HTTP 探活并行执行且使用亚秒级超时，失败只改变启动摘要，不阻断 Server，
也不改变 Memory 降级、Qwen Chat 或 OpenTelemetry fail-open 语义。控制台不输出依赖 URL、
凭据或底层异常。

对应关系保持明确：

- Gateway 机器事件：`.data/gateway_events.jsonl`
- Gateway 开发者视图：`scripts/gateway_view.py last --follow`
- Gateway 兼容 text projection：`.data/logs/gateway.log`
- AgentRuntime 机器 trace：`.data/graph_trace.jsonl`
- AgentRuntime 开发者视图：`scripts/agentruntime_view.py last --follow`

`scripts/gateway_view.py last` / `latest` 默认锚定最新 Gateway run 或 trace 事件；
run 之后追加的 `gateway.session.destroyed` 等 session lifecycle 事件不会抢占 latest
run 视图。需要逐条检查 lifecycle 时使用 `--tail`。

Gateway viewer 和 AgentRuntime viewer 的 `last --follow` 默认持续观察全局 latest
run。每个 observed session 的第一块输出前都会打印 `SESSION` 分割符，方便把消息记录
和 session 对上；`scripts/run_client.py /new`、真实通话重连或并行调试会话都会清楚分段。
需要强隔离时，加 `--session-id <session>`。
Conversation 层要求 server 已显式启用 `--allow-local-trace-content`；共享
`.run/Assistant Server.run.xml` 已为本地调试启用该开关。AgentRuntime viewer 配置本身不保存
密钥或 `.env` 路径。成功 turn 的 Conversation 来自正常 conversation history；失败或
取消的 turn 只在本机 trace-content debug store 中保留当前用户输入和 bounded 失败摘要，
用于开发视图定位问题，不进入后续模型对话历史。

## Realtime Video Observation

Realtime video observation remains visible through the governed internal
`realtime_video_observe` tool and the redacted context projection consumed by
the foreground model. These are distinct boundaries: Qwen still runs behind
`ActionValidator` and `ToolExecutor`, while a trusted Agent-Service turn may
expose `live_view_inspect`; ordinary referenced media exposes `media_inspect`.
Structured diagnostics use these
prompt-safe sources:

- `background_keyframe_observation`: a selected keyframe was analyzed by the
  per-connection observer through `ActionValidator` and `ToolExecutor`;
- `realtime_video_context`: the foreground model consumed the latest rolling
  snapshot on its first context build, with no query-time visual Provider call;
- `rolling_video_memory`: `live_view_inspect` reused the latest healthy
  semantic snapshot;
- `request_image` / `explicit_video`: `media_inspect` analyzed media explicitly
  attached to the current request.

允许记录的视频字段仅限 prompt-safe scalar 或有限枚举：source、opaque video/output
reference、snapshot/target/completed sequence、sequence gap、observed timestamp、
keyframe/queue count、status、reason code、provider/model、transport、session generation、
connection reused、reconnect count、first delta latency、total observation latency、
capture/publish age 和 freshness wait/result。trace、日志、delivery audit 与 context report
一律禁止记录绝对或相对帧路径、Base64/Hex、PCM、JPEG/H.264 bytes、灰度指纹、Qwen 原文、
Provider 请求/响应、raw event body、phone number 或用户可见回答文本。

`videoResponse(code=0)` is an ingestion signal: H.264 validation, JPEG and
fingerprint decode, context registration, and local selection scheduling
completed. It is not evidence that background MLLM observation completed.
Connection cleanup stops scheduling, rejects late semantic updates, then removes
rolling snapshots and both retained and raw JPEG artifacts.

H.264 decode, keyframe selection, observer queue wait, Provider observation,
and snapshot publication run before or outside the chat critical path. The turn
summary projects only the latest semantic snapshot actually consumed by that
turn: source, snapshot/target sequence, sequence gap, frame-capture age,
snapshot-publication age, freshness wait duration/result, observation latency,
queue state, and Provider/model. The latency
projection prefers `context.build.finished.realtime_video`; only non-realtime
foreground tool calls fall back to tool-result projection. Explicit uploaded
video Provider work is inside `tool_execute[media_inspect]` and can become the
turn bottleneck; `live_view_inspect` itself only reads the rolling snapshot.

`context.build.finished` reports only presence, state, snapshot/target sequence,
sequence gap, capture/publish ages, freshness wait duration/result, observation
latency, provider/model, and queue state. A current-camera turn waits at most
1.5 seconds for `snapshot_sequence >= target_sequence`; the same observer keeps
one Qwen in flight plus one latest-wins pending frame. It must
not contain the Qwen summary, raw conversation, frame path, Base64, or media
payload. Agent-Service packet-level received/sent lines are DEBUG; disconnect
emits one aggregate INFO with message/video/byte/failure counters.

## Purpose

The observability harness should answer five developer questions quickly:

1. What happened in this run?
2. Where did it stop, fail, cancel, or wait?
3. Why did the assistant choose a tool or final answer?
4. Which tool/provider/context/memory boundary contributed to the result?
5. Is the trace safe to inspect, return through APIs, and use in tests?

The harness is local-first. Internal event and trace semantics should stay close
to OpenTelemetry-style traces, metrics, and logs, but this project should not
require an external APM stack for normal development.

## Current Surfaces

| surface | owner | purpose |
| --- | --- | --- |
| `AgentState` | runtime | In-memory fact record for one run: status, tool calls, results, errors, and response. |
| `AgentEvent` / `EventSink` | runtime and entry layers | Real-time event stream for WebSocket, Gateway, realtime, CLI, and tests. |
| `TraceStore` / `TraceQueryService` | services | Canonical full-content run trace plus bounded query summaries for `/runs/{run_id}`, `/traces/{trace_id}`, and tool-call debug views. |
| Operational text logs | services / gateway | Combined console plus isolated rotating Gateway lifecycle logs; runtime development uses `agentruntime_view.py last --follow` over canonical trace JSONL. |
| `react_steps` / `decision_trace` | API response metadata | Compact per-response ReAct timeline for developer UI and CLI output. |
| `RunHistoryStore` / `SessionStore` | services | Local JSONL/session indexes and lifecycle ledgers. Tool-call history queries are derived from the canonical trace timeline. |
| Gateway frames | gateway | Realtime wire lifecycle: `run.started`, `event.progress`, `stream.chunk`, `run.end`, `run.cancel`, call hangup, and config updates. |

These surfaces may expose the same fact at different fidelity. The canonical
source for debug reconstruction and trace-driven evaluation is the full trace
timeline. API
response fields and realtime events are projections of that timeline or live
runtime lifecycle events.

Product entry layers should reach assistant execution through
`AssistantRuntimeApp` before entering `run_assistant_request` and
`AgentGraphRuntime`. Hook observers should attach after this application
runtime boundary is stable; they should not be wired separately inside Web,
CLI, or Gateway transport adapters.

For local composition, `CompositeEventSink` can fan out one runtime event stream
to several sinks, and `CompositeTraceStore` can fan out trace writes to a
primary store plus secondary stores while keeping reads primary-only. These are
observer composition primitives, not a generic HookManager and not interception
points for changing assistant behavior.
`HookManager` builds on those composition primitives as an observer-only
vocabulary layer. `HookEventSink` forwards `AgentEvent` records to observers,
and `HookTraceStore` forwards trace writes to observers when used as a secondary
store. Hook observers cannot intercept or mutate assistant behavior; they only
receive prompt-safe lifecycle records and hook dispatch errors.
`TraceMetricsObserver` is the local in-memory metrics observer for this hook
layer. When attached to `HookManager` through `HookTraceStore`, it stores
redacted trace events and exposes the same aggregate shape as
`build_trace_metrics()`. It is a developer harness helper, not a metrics
exporter, dashboard, policy hook, or API surface.
`TraceInvariantObserver` is the local in-memory audit observer for this hook
layer. When attached to `HookManager` through `HookTraceStore`, it stores
redacted trace events and reports prompt-safe `TraceInvariantViolation` records
for broken run/tool sequencing or unredacted hook dispatch errors. It is
passive: violations are inspected after a run or test, and the observer does
not raise, cancel, export, or mutate runtime behavior.

## Canonical IDs

Every run-observability record should carry the strongest available identity
chain:

```text
user_id
session_id
run_id
trace_id
span_id
parent_span_id
tool_call_id
turn_id
```

Required fields by layer:

- Run-level events require `run_id`, `trace_id`, `user_id`, and `session_id`.
- Tool events require `tool_name` and `tool_call_id` when a call has been
  allocated.
- Gateway/realtime events should preserve `turn_id` when available and include
  backend `run_id` / `trace_id` in terminal frames or payloads.
- Cross-agent or delegated work must preserve parent `run_id` / `trace_id`
  through the agent communication boundary.

`trace_id` is the cross-surface debug lookup key. `run_id` identifies one
assistant execution. `span_id` and `parent_span_id` are the future-proof local
shape for hierarchical traces and optional OpenTelemetry export.

新生成 ID 统一由 `services.identifiers.IdFactory` 负责：

| identity | new format | lifecycle |
| --- | --- | --- |
| `run_id` | `run_<uuid7-hex>` | 从入口到 Assistant Runtime 的一次执行 |
| Runtime `session_id` | `session_<uuid7-hex>`；外部可信 session 可保持原值 | 多轮会话 |
| Gateway `turn_id` | `turn_<uuid7-hex>` | 一次入口用户轮次 |
| `delivery_id` | `delivery_<uuid7-hex>` | 一次 Agent-Service 投递与 ACK |
| `tool_call_id` | `tool_call_<uuid7-hex>` | 一次工具调用 |
| `trace_id` | 32 lowercase hex / 128 bit | W3C Trace Context trace identity |
| `span_id` | 16 lowercase hex / 64 bit | W3C Trace Context span identity |

UUIDv7 business IDs preserve opaque identity while improving chronological sorting and
database index locality. Trace/span IDs stay random W3C-native values rather than embedding
business identity. ID values must not contain user identifiers, phone numbers, prompt text,
provider names, host names, or other business data. Readers continue treating IDs as opaque
strings and accept historical UUIDv4/prefixed values; existing persisted records are not rewritten.

## Canonical Timeline

Use stable dotted event names for the internal trace timeline. Existing
`AgentEvent`, Gateway frame names, and response metadata can keep their current
public names, but they should map to this vocabulary.

| canonical event | meaning |
| --- | --- |
| `run.started` | A user request has entered the assistant runtime. |
| `gateway.turn.started` | Gateway accepted a normalized user turn. |
| `memory.session_recall.finished` | Session 创建时唯一一次 Mem0 长期记忆召回。 |
| `context.build.started` / `context.build.finished` | Prompt/native context pack construction and budget report. |
| `llm.chat.started` / `llm.chat.finished` | One provider chat call, including direct-answer and native tool-call responses. |
| `assistant.output` | Strict assistant turn output selected: non-empty text or a tool call. |
| `action.validation.started` / `action.validation.finished` | Local validation before tool execution. |
| `tool.started` | Tool execution lifecycle began through `ToolExecutor`. |
| `tool.finished` | Tool returned successfully, including duplicate suppression. |
| `tool.failed` | Tool failed, budget was blocked, or cancellation interrupted execution. |
| `tool.attempt.failed` / `tool.retry.scheduled` | One execution attempt failed and a retry was scheduled inside the same logical Tool call. |
| `tool.observation` | Tool result was converted into assistant-facing observation data. |
| `loop_guard.triggered` | ReAct guard blocked, finalized, or terminated according to its explicit disposition. |
| `runtime.phase.changed` | Runtime changed between explicit foreground phases, currently `ACT -> FINALIZE`. |
| `response.delta` | User-visible response text chunk was emitted. |
| `response.final` | Final response was set. |
| `response.delivered` | Realtime/entry recorded the final text actually delivered to the client. |
| `memory.ingestion.queued` / `memory.ingestion.finished` | Post-response Mem0 ingestion lifecycle. |
| `run.completed` | Run ended successfully. |
| `run.failed` | Run ended with an error. |
| `run.cancelled` | Run ended through cooperative cancellation. |

The existing mock/offline LangGraph node path remains useful, but node names are
not the canonical event vocabulary. Native provider runtime must produce the
same high-level timeline even when it does not enter LangGraph node wrappers.
`ToolExecutor` is the canonical owner for `tool.started`, `tool.finished`, and
`tool.failed`; runtime layers may still emit `tool.observation` after converting
the result into assistant-facing data.
`AssistantContextPack` construction is wrapped by the context observability
helper, which emits `context.build.started` and `context.build.finished` with
redacted budget, source-count, compaction, and tool-catalog summaries.
Memory lifecycle calls are wrapped at the runtime boundary:
`memory.session_recall.finished` 只记录 session-start recall 的终态；
`memory.ingestion.queued` / `memory.ingestion.finished` 记录非阻塞
Mem0 add。事件不包含原始 user/assistant 文本或 Mem0 响应，也不维护第二份 memory operation overlay。
每轮是否注入冻结 snapshot、注入数量和字符数属于 `context.build.finished`。
Final response tracing emits `response.final` with only prompt-safe response
shape data such as message presence, character count, output-ref count, response
data keys, status, and error count. It must not include the response text.
Realtime delivery additionally emits `response.delivered`。The redacted event stores only
source、presence 和字符数；`RealtimeAgentResult.response_text` 与 Runtime 最终文本保持一致，入口层
不得根据工具结果替换正文。Langfuse root output 使用实际交付文本，`response.final` 保留同一份
Runtime/模型最终正文；两个事件用于区分生命周期，不表示存在第二套回答生成逻辑。

## Span Model

Trace records should move toward a span-plus-event model:

```text
run
  gateway.turn
  memory.load
  context.build
  llm.chat(iteration=1)
  action.validation
  tool.execute(shopping_search)
    provider.call(shopping_search.search)
    provider.call(shopping_search.compare)
  tool.observation
  llm.chat(iteration=2)
  response.final
  memory.save
```

Minimal span fields:

```text
span_id
parent_span_id
name
event_type
status
started_at
finished_at
duration_ms
attributes
error
```

`TraceEvent.observation_type` 是 trace 到 OpenTelemetry/Langfuse 的通用导出契约，取值为
`span`、`generation` 或 `event`；可选 `observation_name` 用于覆盖由 canonical event
推导出的展示名称；`observation_scope` 声明它直属 `runtime` 还是当前 `iteration`。
业务埋点决定一个事件是否构成 observation，并通过 `span_id`、`parent_span_id`、scope 和
`attributes.iteration` 声明层级。OTel mapper 只消费这些结构化字段，
不得维护 tool、memory 或其他业务事件名 allowlist。新增 `memory.daily.append.finished` 一类
operation 时，只需在事件生产处声明 observation 语义，不需要修改 Langfuse exporter。
同一 `span_id` 存在配对的 canonical `*.started` / observation finished 事件时，导出 Span
必须分别使用 started 事件和 finished 事件的真实 `created_at` 作为起止时间，不能用整数毫秒
`latency_ms` 反推并覆盖真实起点。只有旧 trace、partial trace 或自定义埋点缺少配对 started
事件时，才使用 `latency_ms` 反推起点作为兼容降级。
未声明 `observation_type` 的 timeline、summary 和 bookkeeping 事件仍保留在本地 trace，
但不会被误导出成 observation。

`attributes` may contain full evaluation evidence. Low-cardinality operational
metrics should still use dedicated structured attributes rather than raw content
as metric labels.

## Developer UX

The harness should optimize for a developer debugging locally.

### 真实 `assistant.turn` 定位与诊断

用户提供 `assistant.turn: <32 位十六进制 ID>` 时，该 ID 默认按 Langfuse
`assistant.turn` trace 的 `trace_id` 处理，不按 observation id、run id 或自由文本搜索词处理。
Agent 应立即使用精确 ID 查询当前环境的 Langfuse Public API，并并行检查最新 `.data/**`
机器日志；不得等待本地 JSONL 命中后才查询 Langfuse，也不得先用 mock 复现、经验判断或旧
trace 替代这次真实运行。

精确定位流程如下：

1. 从本机未跟踪配置读取 Langfuse host 和凭据；host 默认使用
   `http://localhost:3000`，查询 `GET /api/public/traces/{trace_id}`。凭据只用于认证，
   不得打印到命令输出、分析结果或文档。
2. 同时用该 ID 检查 `.data/**`，并读取与问题相关的最新 Gateway、Agent-Service、
   graph trace、eval artifact 或其他机器日志。Langfuse 精确 trace 是已持久化的机器事实；
   `.data/**` 用于补充本地生命周期、传输和未导出事件，二者不要求互相命中后才能继续。
3. 开始行为分析前，确认响应中的 trace `id`、名称 `assistant.turn`、timestamp，以及 metadata
   中可用的 `assistant_trace_id`、`run_id`、`agent_session_id` / `session_id`。若用户同时提供
   其他标识，必须先确认它们属于同一次运行。
4. 精确 trace 命中后，以该 trace 的 observations、时间和关联 ID 为主要证据，再结合对应
   `.data/**` 日志与源码解释。回答应注明所依据的 trace ID、时间和 run ID；存在相关本地日志时
   同时注明文件。
5. 当前 Langfuse 不可达、无权限或查无此 ID 时，再降级到 `.data/**` 按 trace/run/session
   关联定位，并明确说明缺失了哪一层证据。当前环境的 Langfuse 和本地日志都无法对应时，才请求
   用户补充环境或 Langfuse host；仅有标准 `assistant.turn: <trace_id>` 且当前 Langfuse 可查询时，
   不要求用户额外提供 run ID、session ID 或时间。

Current query surfaces:

```bash
GET /runs/{run_id}
GET /traces/{trace_id}
GET /runs/{run_id}/tool-calls
GET /traces/{trace_id}/conversation  # explicit loopback debug only
```

Local CLI:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last --errors
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last --follow
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last --follow --follow-include-existing
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last --follow --follow-live-updates
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last --sections decision
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last --sections timeline
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last --trace-path .data/graph_trace.jsonl --server http://127.0.0.1:8000 --sections overview,conversation --errors --follow
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last --trace-path .data/graph_trace.jsonl --server http://127.0.0.1:8000 --sections overview,conversation,decision,timeline --latency-stages
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py <run_id-or-trace_id>
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py <run_id-or-trace_id> --errors
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py <run_id-or-trace_id> --json
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py <run_id-or-trace_id> --server http://127.0.0.1:8000
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py --trace-path .data/graph_trace.jsonl
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py --json
```

`last`、`latest` 和 `@last` 在一次性查看时解析为本地 JSONL 文件中最后活跃的 run，
避免从控制台复制 `run_id` / `trace_id` 才能打开详情。`--session-id` 会先过滤本地
JSONL，再解析 `last` 或驱动 `--follow`，适合明确只看某个调试会话。若不传
`--session-id`，`last --follow` 会持续跟随全局 latest run。它默认从启动时已有记录
之后开始等待新 trace，不会首屏打印旧 run，也不会把同一 turn 的中间 trace 快照
打印成多块。每个 observed session 的第一块输出前都会打印 `SESSION` 分割符，帮助区分
`run_client /new`、真实媒体新连接或并行调试会话。
默认只在 run 达到终态时输出一次；新 trace 优先等待
`assistant.turn.summary`，旧 trace 继续用 raw `run.completed` / `run.failed` /
`run.cancelled` 和 `agent_service.turn.finished` 兜底。Agent-Service summary 在
`agent_service.turn.finished` 之后追加，确保 Turn Overview、Conversation、
Decision Trace 和 Raw events 尽量完整。Turn summary 作为终态/session 选择的机器事实保留在
payload/JSON 中，不在 human view 里单独输出。需要立即打印当前 latest 再继续跟随时，显式加
`--follow-include-existing`；需要逐事件观察中间态时，显式加 `--follow-live-updates`。
`--follow-all-sessions` 和 `--show-session-banner` 保留为兼容参数；现在默认已经是
跨 session follow，并且默认打印 session banner。

Agent-Service 入口失败会立即追加 `agent_service.turn.finished`，因此默认 follow
可以展示 partial trace。Turn Overview 会把入口超时显示为
`execution=pending_cancel delivery=failed task_outcome=unknown ux_outcome=failed`，
并在 Turn latency 中列出 `gateway_turn_timeout` 与未闭合的 `active_stage`。

server 参数和 AgentRuntime viewer 参数分开理解：`scripts/run_server.py` 的参数负责启动
runtime、mock provider 和日志；trace content 默认开启，旧
`--allow-local-trace-content` 只作为兼容参数保留，显式设置
`MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT=0` 才关闭内容采集；
`scripts/agentruntime_view.py` 的 `--trace-path`、`--server`、`--sections`、`--follow`、
`--session-id`、`--follow-all-sessions` 和 `--show-session-banner` 只负责查询与展示。`--follow --server` 的数据流是：本地
`.data/graph_trace.jsonl` 发现当前 session 最新 trace 或变化，再用 `trace_id` 向
loopback server 拉 `/traces/{trace_id}`；包含 `conversation` 时，再拉
`/traces/{trace_id}/conversation`。server 查不到 trace 时降级使用本地 summary；
conversation endpoint 查不到时，查看器会从默认 `.env`（可用 `--env-file` 覆盖或
`--no-env-file` 禁用）加载 `LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY`，默认通过本机
`http://localhost:3000` 的 Langfuse Public API 读取已持久化
trace；两处均不可用时才标记 unavailable，仍继续输出 Turn Overview。失败 run
如果有本机 trace-content debug 记录，Conversation 会显示用户输入和“请求失败/已取消”
摘要；该记录不写入普通 conversation history，不作为未来 prompt 上下文。
需要强隔离时再加 `--session-id <session>`。

`--sections` 控制输出层级：默认是 `overview`。`conversation` 需要 `--server` 且
trace content 未被显式关闭；`decision` 按 ReAct iteration
聚合决策和工具结果；`timeline` 展示 Raw events。`react` 和 `--react-detail` 仍作为
兼容输入接受，并映射到 `decision`。server-backed view 在 Overview 中聚合
`turn_latency`、LLM wall/provider 差值、tool latency、Context peak、ACK state 和
缺失的实时用户感知指标；需要在 `Turn latency` 中展开旧版 stage rows 时再加
`--latency-stages` 并显式请求 `timeline`。Conversation、LLM、Tool 和 Memory 证据写入
每个 run 的 `trace.content` 事件并持久化到 JSONL。

推荐 PyCharm 本地流程：

1. 运行 `.run/Mem0.run.xml`，等待控制台输出 `Mem0 ready`。该配置随后结束，但记忆服务继续运行。
2. 运行 `.run/Langfuse.run.xml`，等待控制台输出 `Langfuse ready`；需要持续保留该 Run 进程。
3. 运行 `.run/Assistant Server.run.xml`。Trace content 默认开启，并持续写
   `.data/gateway_events.jsonl`；只有显式设置内容环境变量为 `0` 时 Conversation 层不可用。
4. 运行 `.run/Gateway.run.xml`，它常驻输出 Gateway server、session、queue、
   run、cancel 和 interrupt lifecycle。
5. 运行 `.run/AgentRuntime.run.xml`，它全局常驻输出
   Turn Overview -> Conversation，并在 session 切换时打印单行 banner。需要看执行路径时
   临时运行 `--sections overview,decision`；需要查事件状态机时再运行 `--sections timeline`。
6. 需要文本联调时运行 `.run/Assistant Client.run.xml`。

共享 `.run` 配置与 `.run/Assistant Server.run.xml` 对齐为
`http://127.0.0.1:8089`。实际通话测试如果本机 server 跑在其他端口，复制对应
PyCharm 配置到个人配置后再修改 `--server`，不要把个人端口写回共享配置。

For a one-off local debug view, restart the server with the explicit gate and
request only the matching turn:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --provider mock --image-provider mock --allow-local-trace-content
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py last \
  --trace-path .data/graph_trace.jsonl \
  --server http://127.0.0.1:8000 \
  --session-id pycharm-debug-session \
  --sections overview,conversation,decision \
  --errors
```

显式请求 `--sections conversation,...` 会触发 conversation lookup；
`--include-conversation` remains as a compatibility shortcut for adding the
conversation section when `--sections` is omitted. Conversation lookup rejects
non-loopback server URLs. The server endpoint is disabled by default, checks the
socket peer, joins the existing `ConversationStore` by trace identity, returns
only the current user/final-assistant pair, and clips each side to 1000 Unicode
characters with an explicit truncation marker. Do not enable this gate on a
shared or production process.

Human-readable default output should show the diagnosis first:

```text
run run_xxx trace trace_xxx status=completed events=55 errors=0 duration=27790ms

Turn Overview
  execution=success delivery=success task_outcome=unknown ux_outcome=unknown

Performance
  Total latency    27790ms
  First response   first_text=unknown
  web_search       x3 8560ms
  web_fetch        x2 3470ms
  LLM chat x4      14624ms
  LLM wall         7085ms provider=245ms overhead=6840ms
  Context peak     81.3%

Decision path
  LLM chat x4
  Decision tool_call web_search
  Tool web_search x3
  Tool web_fetch x2

Main issues
  P0 LLM overhead 6840ms exceeds provider latency
  P1 Context peak 81.3%
  P1 first text latency is missing

Suggested actions
  1. Break down LLM queue, request build, TTFT, stream consume, parse, and finalize timing.
  2. Inspect system prompt, tool schemas, and tool observations as primary context contributors.
  3. Record first text response latency for text turns.
```

Developer output should group errors first when requested, then show the full
diagnostic layer requested by `--sections`. The default view should be short enough to paste into an issue or a
handoff note. Metrics output should answer aggregate health questions without
opening individual traces first.

## Metrics

The first metrics layer should remain small and derived from trace/events where
possible:

| area | metrics |
| --- | --- |
| Run | count, success rate, failure rate, cancel rate, duration p50/p95. |
| Tool | call count by tool, latency, failure rate, retry count and category. |
| LLM | chat call count, latency, token usage, native tool-call rate, direct-answer rate, provider error count. |
| Context | total chars/tokens, budget ratio, compaction triggered count, overflow retry count. |
| Gateway/realtime | active runs, interrupt count, cancel source, hangup cancellation, deadline expiry. |
| Memory | session recall count/status, snapshot reuse count, background ingestion count/failure rate. |

Do not add high-cardinality labels such as raw prompts, raw queries, full URLs,
full memory text, full provider errors, or media payloads.

The local metrics command currently exposes this first layer:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py
```

It reads the full-content JSONL trace store, supports optional `--user-id` and
`--session-id` filters, and prints either a paste-friendly text summary or a
machine-readable `--json` summary. API/debug endpoint exposure should be added
only after the local metrics shape is stable. Tool metrics prefer terminal
`tool.finished` / `tool.failed` lifecycle events and only count
`tool.observation` for older traces that lack terminal tool events.
For in-process hook composition, `TraceMetricsObserver` exposes the same metrics
shape from trace events received during the current process. It is useful for
tests and local harness wiring where reading the JSONL trace store is
unnecessary.

## Trajectory Debug

`assistant_agent.observability.trajectory_debug` provides the Phase 5 local
trajectory debug contract. It converts already-redacted `TraceEvent` records
into `TrajectoryReplayCase` objects for debug, replay preview, and regression
eval review.

Trajectory replay cases are diagnostic artifacts only:

- `replay_mode` is `debug_replay_eval_only`.
- `production_mutation_allowed` and `raw_data_included` are always false.
- Timeline entries keep only prompt-safe IDs, event names, statuses, tool names,
  provider/model labels, error codes, latency, span links, and allowlisted
  budget/retry/output-ref summaries.
- They do not include raw user text, prompts, rendered context, raw memory
  content, raw provider payloads, authorization values, or inline media bodies.

`evaluate_trajectory_improvement_gate(...)` is a manual-review gate, not a
learning loop. A trajectory-derived memory or skill improvement can enter manual
review only when the replay case is redacted and the target regression suite has
passed. The report never permits automatic production policy, memory, skill,
prompt, routing, tool, or provider changes.

The offline Improvement Lab builds on this diagnostic boundary without changing
it. `scripts/run_improvement_lab.py` accepts explicit run/trace IDs and
structured prompt-safe eval/test failure reports, converts them into versioned
evidence, detects recurring opportunities deterministically, and produces
evaluated skill/runtime/code proposals for human review. It does not run from
`AgentGraphRuntime`, expose an assistant tool or API route, or permit production
mutation. Semantic skill proposals require structured eval evidence; sparse
trajectory timelines remain suitable only for operational failure signals.

## Content Capture Rules

Trace content is retained by default. The following transport secrets and
non-evaluation payloads remain excluded:

- API keys, tokens, Authorization headers, cookies, or secret-like strings.
- Vendor SDK envelopes and raw HTTP request/response bodies.
- Hidden chain-of-thought or internal reasoning fields.
- Base64, inline media payloads, and binary audio/video/image bodies.

`TraceStore` 不再执行写入时兜底脱敏；上述排除责任位于 Provider、Tool 和事件生产边界。
自定义 Runtime/事件生产者若把凭据放入 `TraceEvent`，内容会被原样持久化和导出。

默认 OTLP export 对本地和远程 endpoint 都保留内容。启用 OTLP export 时，每个
`llm.chat` generation input 自动接收
Provider adapter 传给 `chat.completions.create(**payload)` 的完整 payload。该对象不经过字段挑选、
摘要或裁剪，因而会原样保留实际的 `model`、`messages`、完整 tool schemas、
`tool_choice`、实际 token 参数名、`stream`、`stream_options` 和 Provider 特有的 `extra_body`。
它是 SDK 调用参数，不是序列化后的 HTTP 字节流，也不包括 Authorization header。

`MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT` 默认开启；显式设为 `0` 才关闭内容采集。它控制
Langfuse root observation、`response.final`、`response.delivered`、generation output 和
`trace.content` 持久化事件。generation output
以 OpenAI-compatible assistant message 保留 Provider 的
原始语义回复（正文、工具调用、拒绝/错误），不把结构化 tool call 改写为展示文本；
finish reason 保留在 trace/协议快照，usage 保留在独立 observation attributes 中，不拼接到 output 文本。
`MULTIMODAL_AGENT_LOCAL_PROVIDER_PROTOCOL_CAPTURE` 同样默认开启，显式设为 `0` 才关闭。
开启时 `provider_protocol_response` 还保存
原始 content、原始工具参数字符串、refusal、finish reason、usage、可用时的 Provider request id
和流式 delta 计数。它不是 vendor SDK 原始响应，不包含 SDK envelope、HTTP header、stream chunk
body 或 `reasoning_content`，并按
对应 `llm.chat` 的 `span_id` 配对，不能只按 iteration 取第一条。内容来自独立的进程内
`TraceConversationStore`，用户/助手单侧
OTel 单侧最多导出 4000 字符；`trace.content` 使用更高的持久化上限。上一轮 Provider 的 hidden reasoning 字段始终替换为 `[redacted]`；
stream callback 不进入该 store。protocol snapshot 优先用于生成精确
output preview；缺失时从归一化 `ChatResult` 重建完整语义回复。`runtime_route` 记录归一化结果触发的
实际 `fallback | tool_governance | text` 动作并留在 metadata。上述正文同时进入
`.data/graph_trace.jsonl` 的 `trace.content` 事件。
普通 user/assistant content 经 `TraceConversationStore` 完成正文限长后，由 OTel mapping
逐字符导出，不得复用 error sanitizer 压平空白或再次按 300 字截断。`llm.chat`、
`response.final`、`response.delivered` 和 root 分别保留 Provider、Runtime final、
实际 delivered 和 turn output 的事实来源。

Langfuse 的工具链同样使用完整视图：`tool.execute` 直接显示执行 trace event 当前持有的
完整 input/output summary，不再只挑 field count、result count 和 output ref，也不在 OTLP observer
或 mapping 层再次执行 sanitizer；每个
`tool.observation` 还会在独立的进程内 store 保存完整 `ToolObservation`，Langfuse 按
`observation_index` 投影该对象，原样展示 `status`、`summary`、`outcome`、`warnings`、
`is_complete`、工具专属 `data`、结构化 `error` 和 `output_ref`。因此它与 assistant loop 产生的
模型观察是同一对象，而不是观测层再次生成的摘要；观测对象不保留 `structured_output`、拆分错误、
命令式恢复提示或恒定 redaction 标记。完整对象也进入
`.data/graph_trace.jsonl` 的 `trace.content` 事件。Langfuse 的 `tool.execute` 仍展示完整执行结果，
但不再重复嵌入 `model_observation`；该模型可见投影只在 `tool.observation` 展示。
成功或失败执行产生的 `tool.observation` 同时携带 executor 分配的 `tool_call_id` 和
`source_tool_span_id`，用于与对应 `tool.execute` 建立确定关联；validation rejection 等未进入
executor 的 observation 不伪造工具执行关联。
模型在 tool-call turn 同时返回的可见文本进入 `tool_started.text`，Gateway 将其作为
`run.progress.message`；若该文本为空，Gateway 继续生成 `Calling <tool>.` 的 Runtime progress，
两种情况都不把工具前言混入最终回答流。

记忆链路只保留最小生命周期事件。session 创建阶段的 `memory.session_recall` 显示状态、
数量和错误码；turn 内不产生 memory lifecycle event，冻结 snapshot 的注入事实由
`context.build` 展示。最终
`llm.chat` generation input 可用于确认冻结文本已作为 user evidence 进入 Provider 请求。
回复提交后的 `memory.turn_ingestion` 独立记录后台 Mem0 add 的结果，不存在 memory tool
observation 或观测层自建的 memory operation overlay。

默认内容策略也允许 ToolHistory 和工具 trace 保存工具输入输出，并适用于真实 Provider
smoke/pilot 证据采集；仍排除 Authorization、API key、hidden reasoning、vendor SDK envelope
和内联二进制内容。

Trace data includes:

- IDs, statuses, event names, error codes, recoverability, and component names.
- User requests, assistant responses, model messages, and tool input/output.
- Memory lifecycle status and counts.
- Provider/model names when not secret.
- Latency, token counts, retry count, budget summaries, side-effect/risk gate summaries.
- Output references and artifact IDs.
- Context budget and compaction summaries.

Full-content persistence is part of the harness contract. Tests should fail if
trace output loses evaluation evidence, or contains credentials, hidden reasoning,
vendor SDK envelopes, or inline binary media.

## Harness Invariants

Regression tests should enforce these invariants:

- Every `run.started` has exactly one terminal `run.completed`, `run.failed`, or
  `run.cancelled`.
- Every `tool.started` has a matching `tool.finished` or `tool.failed`.
- Every `tool.observation` references a prior tool call or a validation rejection.
- Native provider runtime and mock/offline ReAct runtime both emit
  internal `assistant.output` trace facts and terminal run events. `assistant.output`
  remains available to local trace evaluation but is not exported as a separate
  Langfuse/OTel observation because `llm.chat` already shows the Provider reply.
- Successful native provider runtime and mock/offline ReAct runtime both emit
  `response.final` before the terminal run event.
- Successful runs may enqueue `memory.turn_ingestion` after the terminal response;
  ingestion failure never changes the already returned run result.
- A validation rejection never enters `ToolExecutor`.
- A failed tool carries error code, source, recovery action, and redacted message.
- Cancel, interrupt, timeout, and hangup traces include their source.
- `/runs/{run_id}` and `/traces/{trace_id}` expose only redacted summaries.
- No public trace, API response, or realtime trace event exposes hidden reasoning.
- Local hook auditing can use `TraceInvariantObserver` to check the first local
  invariant set in-process: run terminal events, tool terminal events,
  `tool.observation` provenance, failed-tool error detail, and hook dispatch
  error redaction.
- The default pytest safety net does not enumerate trace internals. Add a minimal regression only for a
  concrete redaction, terminal-event or public trace-contract defect. Broader evidence comes from local
  trace smoke scripts and machine logs.
- Future work should be driven by a concrete debugging gap rather than adding more event types, dashboards,
  exporters, debug endpoints or assertion matrices preemptively.

## Phase Plan

### Phase 1: Align Trace Semantics

- Add canonical event names to trace summaries without breaking existing public
  response fields.
- Make native provider runtime write `llm.chat`, `assistant.output`,
  `action.validation`, `tool.observation`, and terminal run events to
  `TraceStore`.
- Keep existing LangGraph node trace for mock/offline debugging, but map it into
  the canonical timeline.
- Add invariant tests for native and mock/offline runtime paths.

### Phase 2: Developer AgentRuntime Viewer

- Add `scripts/agentruntime_view.py`.
- Support lookup by `run_id` or `trace_id`.
- Provide default, `--errors`, and `--json` output modes.
- Keep output redacted and paste-friendly.

### Phase 3: Metrics Summary

- Add a local metrics summary derived from trace/events.
- Expose run/tool/LLM/context/Gateway/memory counters through
  `scripts/trace_metrics.py`.
- Defer debug endpoint exposure until the local metrics contract is stable.
- Keep labels low-cardinality and safe.

### Phase 4: Optional Export

- Add optional OpenTelemetry-compatible export only after local harness semantics
  are stable.
- Export should be disabled by default; once enabled it exports full trace
  content under the same credential/hidden-reasoning/binary exclusions.
- The Python packages live behind the `observability` extra:
  `assistant_agent[observability]` installs `opentelemetry-api`,
  `opentelemetry-sdk`, and `opentelemetry-exporter-otlp-proto-http`.
- Runtime OTLP export is opt-in through environment variables only. 本地 Langfuse 只需
  `ASSISTANT_AGENT_OTEL_EXPORT_ENABLED=true`、`LANGFUSE_PUBLIC_KEY` 和
  `LANGFUSE_SECRET_KEY`；代码默认使用 `http://localhost:3000/api/public/otel/v1/traces`、
  由凭据生成 Basic auth header、`assistant-agent-local` service name、5 秒 timeout 和 1024
  queue capacity。特殊部署仍可使用 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`、
  `OTEL_EXPORTER_OTLP_TRACES_HEADERS`、`OTEL_SERVICE_NAME` 和
  `ASSISTANT_AGENT_OTEL_EXPORT_QUEUE_CAPACITY` 覆盖。If only generic
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set, the text trace exporter derives the
  trace endpoint by appending `/v1/traces`.
- Root、`llm.chat`、`tool.execute`、`tool.observation` 和
  `response.final` 使用 Langfuse 的 `langfuse.observation.input/output` 映射结构化 JSON；
  root 同时写入 `langfuse.trace.input/output`。默认显示完整 tool execution summary 与
  assistant-facing `ToolObservation`；显式关闭 content capture 时才退化为摘要。
- Observation 导出由 `TraceEvent.observation_type/name/scope` 驱动，iteration 父子关系由
  scope 与 `attributes.iteration` 驱动；Langfuse/OTel 映射层不枚举 canonical event。新增工具沿用
  `ToolExecutor` 的统一 lifecycle 即自动获得 `tool.execute` span。现有
  `memory.session_recall.finished` 与 `memory.ingestion.finished` 在 Langfuse 中分别展示为
  `memory.session_recall` 和 `memory.turn_ingestion`。canonical timeline 名称不因展示层命名而改变。
- `llm.chat` generation 默认不设置 `langfuse.observation.output`；Provider/model、usage、latency 和
  attempt kind 使用独立 observation attributes，finish reason 保留在 trace/协议快照。本地 OTLP
  export 自动从进程内 `TraceConversationStore` 按 span id 投影 adapter 捕获的完整 SDK 调用参数；
  input 不做字段重建或展示性重写。启用 local trace content 后，output 使用带
  `role/content/tool_calls` 的结构化 assistant message
  展示 Provider 的原始语义回复，不附加 finish reason 或 usage。协议语义快照还需要独立设置
  `MULTIMODAL_AGENT_LOCAL_PROVIDER_PROTOCOL_CAPTURE=1`。JSONL 只保留 route、transport、terminal
  和 delta count 等安全摘要；这些对象都不是 vendor SDK 原始 envelope。
- canonical `context.build.finished` 在 Langfuse 中展示为 `context.compile`，其 output 明确标记为
  `prompt_safe_context_compilation_report`，并导出 message roles/count、tool count、
  response-format presence 以及 prompt-safe `context_report_v2`：只输出实际出现或发生转换的
  section，并展示 chars、可选 estimated tokens、item count、明确的 compaction kind、trimmed
  和 source。报告分离 precompile estimate、compiled request 和 tokenizer preflight 三种口径；
  token 不可用时标记 `unavailable`，不输出伪造的零值。非空时还展示已选工具、memory ID、
  context source 和 compression 状态。本地开发 overlay 还附带最终 memory
  section 的注入状态、ID 和实际渲染文本。`build_reason` 区分 iteration 初始编译、压缩后重建和
  Provider overflow retry；同 iteration 后续 compile 会取代较早的候选报告。output 中的
  `compiled_request_ref` 以 observation、field 和 iteration 指向对应 `llm.chat.input`；完整 compiled
  `ChatRequest` 仍只放在那里，作为 Provider 调用边界的最终事实。`assistant.output`
  只记录归一化决策，不再复制 `context` 或 `context_report_v2`。`agent.runtime`
  根 span 最多保留 `context_peak_ratio` 这一 turn-level 压力摘要，不携带完整 section
  accounting 或 Provider input。
- 未实现 request callback 的自定义 adapter 使用编译后 `ChatRequest` 的语义字段作为 fallback；
  内置 OpenAI-compatible adapter 必须以传给 SDK 的同一 payload 覆盖该 fallback。
- Langfuse Trace 名称固定为 `assistant.turn`，observation hierarchy 固定为
  `agent.runtime -> react.iteration[n] -> context.compile/llm/decision/tool`，避免把
  Trace 名称再次导出成同名根 observation。memory、final response 和
  runtime postprocess 直接归属 `agent.runtime`。`agent_service.turn.finished`
  是入口延迟汇总事实，不再映射成一个与 root 几乎完全重叠的长 Span；其关联 ID、
  terminal 状态和诊断元数据合并到 root/turn summary。
- `langfuse.user.id` 继续映射 Runtime `user_id`；`langfuse.session.id` 映射内部
  AgentSession id。OTLP metadata 同时写入 `agent_session_id` 和 `session_scope`。
  Agent-Service 当前为 `session_scope=agent_service_connection`，表示该 session 是
  WebSocket 连接级逻辑会话，不能被解释成 vendor `sessionId` 或跨重连 conversation。
- 远程 OTLP endpoint 不导出上述原始 generation input；本地 endpoint 无需额外 content 开关。
- Disabled export must not import OpenTelemetry packages. Missing optional
  dependencies, missing endpoint, full queues, and exporter exceptions are
  observability failures only; they must not block local trace persistence or
  assistant turn delivery.
- 新生成的本地 `trace_id` 本身就是符合 W3C 的 32 位 lowercase hex，Runtime、Gateway、
  Langfuse span 与 Langfuse Score 直接复用同一值，不再二次哈希。历史 `trace_*` 或其他
  非 W3C ID 继续通过 `SHA-256(seed)[:16].hex()` 确定性映射，保证旧 Trace 和已有 Score
  链接不失效。原始值仍保留在 `langfuse.trace.metadata.assistant_trace_id`。
- Text Agent export design and phased execution live in
  `docs/development/text-agent-otel-langfuse-observability.md`; it deliberately
  excludes audio, TTS, playback, speech, and dead-air metrics.

### Text Turn Scores

Text turn evaluation scores are separate from OTLP span attributes. When
`ASSISTANT_AGENT_LANGFUSE_SCORE_ENABLED=true` and Langfuse API configuration is
present, the server appends an optional Langfuse score observer secondary after
local JSONL persistence. It uses the same `TurnDiagnostic` evidence as Turn
Overview and OTel metadata, writes only prompt-safe values, and never writes a
score when `task_outcome=unknown`.

Supported built-in score names are:

- `assistant_agent.task_outcome`;
- `assistant_agent.prerequisites_resolved`;
- `assistant_agent.clarification_too_late`;
- `assistant_agent.unnecessary_tool_calls`.

Score writing is disabled by default. 本地启用只需
`ASSISTANT_AGENT_LANGFUSE_SCORE_ENABLED=true`、`LANGFUSE_PUBLIC_KEY` 和
`LANGFUSE_SECRET_KEY`；host 默认是 `http://localhost:3000`，timeout 和 queue capacity
分别默认 5 秒和 1024。特殊部署可使用 `LANGFUSE_HOST` 或
`ASSISTANT_AGENT_LANGFUSE_SCORE_URL=<host>/api/public/scores`，以及 optional
`ASSISTANT_AGENT_LANGFUSE_SCORE_TIMEOUT`, and optional
`ASSISTANT_AGENT_LANGFUSE_SCORE_QUEUE_CAPACITY`. Missing credentials, missing
URL, full queues, HTTP failures, or writer exceptions are observability failures
only; they must not block OTLP export, local trace persistence, or assistant
turn delivery.

### Langfuse Agent Experiments

Task 中心的 Agent 评测使用独立 optional dependency `assistant_agent[eval]` 和
`scripts/run_agent_evals.py`，不复用生产 `TurnDiagnostic` score。Git 中的 Task、
Environment、Grader 和校准样本定义回归行为；Langfuse Dataset 只保存已发布请求，Experiment
保存 Dataset Run、Trace 和 item `Evaluation`。

Experiment task 从 Langfuse 当前 observation 读取 W3C trace ID 和 parent span ID，通过
`RuntimeTraceContext` 传入 `AgentGraphRuntime.run_state()`。Runtime 仍产生 canonical
TraceEvent，并在 task 内映射导出到同一条 Experiment trace。普通 API、Gateway、CLI 未传
`RuntimeTraceContext` 时仍自行生成 trace ID，行为不变。

显式 Experiment 必须生成完整 Trace，以及固定的
`agent_eval.dimension.tool_execution/tool_semantics/grounding/response_quality` 四个独立
BOOLEAN Score，不生成 reward 或总通过分。Task 专属
rubric 只用于 `response_quality`，不创建工具专属 Score；每条 assertion 显式记录
`evaluation_method=rule|judge` 和人类可读 `label`，Judge assertion 还记录稳定 `criterion_id`。
Score comment 展示 assertion label，失败时同时展示真实 reason，不能只显示内部 assertion ID。
Score metadata 使用
`assertion.<name>.passed|label|method|criterion_id` 独立标量字段，不传播 rubric、长 reason 或
嵌套大对象。Experiment 返回后还必须 flush 并通过 Scores v3 API 回查四项 Score 已实际落库，
且全部关联到同一个 `experiment-item-task` observation。Dataset 认证、Runtime OTLP export、
Environment validation、Judge、Evidence、Score 写入或 Score 回查失败必须 fail-fast，和普通
server observability 的 fail-open 语义不同。

LLM Judge 不复用 Agent 的 stream、timeout 和 SDK retry 传输策略：Judge 固定非流式，默认 timeout
30 秒、SDK retry 0 次，并可由 Agent eval 专属环境变量或 CLI 参数覆盖。Judge 网络默认使用
`ipv4_direct` 绕过环境代理并强制 IPv4；Provider 只能通过代理访问时，可显式切换为 `environment`。
每个 Judge criterion 必须
生成 `judge.<criterion_id>` evaluator observation，记录 timeout、retry、耗时、通过状态或基础设施
错误；CLI 同时把 Task、evaluation 和 Judge 阶段进度以逐行 JSON 写入 stderr，最终机器结果保留在
stdout。这样长时间运行可以区分 Agent 执行、Judge 判定和外部 Provider 建连等待。

### Phase 5: Trajectory Debug Gate

- Build redacted `TrajectoryReplayCase` artifacts from existing trace events.
- Keep replay/eval local and diagnostic-only.
- Require memory or skill regression evidence before any trajectory-derived
  improvement reaches manual review.
- Do not implement RL, automatic self-modification, private-data training, or
  production policy updates.

### Offline Improvement Lab

- Reuse only redacted `TrajectoryReplayCase` data and explicit structured
  eval/test failure records.
- Keep opportunity eligibility and confidence deterministic.
- Keep proposal generation tool-free and profile-gated; default to a local
  deterministic scaffold.
- Evaluate candidates independently with architecture, evidence, scope, skill
  permission and fixed test-suite gates.
- Treat the evidence window as 30 days by default and report its UTC cutoff;
  normalize offset-free structured timestamps to UTC.
- Force explicitly requested allowlisted suites into a sanitized `mock` provider-mode
  environment. A failed suite blocks review readiness before persistence.
- Keep stable proposal candidates separate from run-scoped immutable evaluation
  and validation records so later runs cannot hide a changed result.
- Persist only local JSONL review artifacts and Markdown reports under `.data/`.
- Never apply a candidate, modify a target, create a branch, or deploy a change.

## Update Rules

- Update this document when run status, trace, event, metrics, Gateway lifecycle
  observability, ReAct trace behavior, or developer trace tooling changes.
- Keep `docs/tool-calling-architecture.md` focused on tool governance and
  lifecycle boundaries; link to this document for observability taxonomy.
- Keep `docs/gateway-architecture.md` focused on Gateway protocol and lifecycle;
  link to this document for cross-runtime trace semantics.
- Do not place current observability architecture only in `docs/development/**`.
  Development plans may reference this document but do not replace it.

# Observability Harness

Last updated: 2026-07-15

This document is the current entry for assistant run status, logs, monitoring,
trace, and ReAct checkpoint observability. It defines the developer-facing
harness contract for understanding one run end to end without exposing raw
provider payloads, full prompts, memory content, secrets, media bodies, or hidden
reasoning.

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

The correlation identifiers are deliberately distinct:

- `delivery_id` identifies media delivery and optional ACK state;
- `gateway_run_id` identifies the Gateway lifecycle wrapper;
- `assistant_run_id` identifies the Assistant runtime execution whose trace
  contains LLM/tool stages;
- `trace_id` is the common lookup key used by trace queries and `trace_view.py`.

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

The versioned `agent_service_turn_latency_v1` summary also exposes only bounded
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

The safe INFO records have this shape and never contain prompts or responses:

```text
turn_latency status=sent trace=trace_x gateway_run=run_g assistant_run=run_a delivery=delivery_x session_turn=2 total=824ms bottleneck=llm_chat[2] bottleneck_ms=410ms share=49.8%
delivery_ack status=acked trace=trace_x gateway_run=run_g assistant_run=run_a delivery=delivery_x session_turn=2 ack_latency=18ms
```

`scripts/run_server.py` enables an in-memory primary trace store plus a bounded
background JSONL writer. Response delivery never waits for JSONL I/O: a full
secondary queue drops observability events and increments its drop counter.
Shutdown attempts a bounded flush. This persistence is local diagnostic data,
not a delivery authority.

## Operational Logging

本地 server 在不改变 FastAPI 单进程结构的前提下提供三层开发视图：控制台是
面向开发者阅读的 Combined 摘要，`.data/logs/gateway.log` 只接收 Gateway lifecycle，
`.data/logs/runtime.log` 只接收 Assistant runtime trace 投影。Combined 默认采用
`concise` 模式，只显示关键 Gateway lifecycle、runtime 失败/取消摘要以及普通应用
WARNING/ERROR 的 logger 名；节点级 runtime 细节仍持续写入文件页签。未经过安全投影的
普通应用 message 不在 Combined 原样显示，即使 `verbose` 也只显示其 logger 元数据。
控制台 INFO/DEBUG 写 stdout，WARNING/ERROR 写
stderr，避免 PyCharm 将所有正常事件渲染为红色；控制台只显示短关联 ID，不显示稳定
身份摘要或密集 payload。Uvicorn 内建 INFO 默认降噪，避免 WebSocket 握手 query value
绕过安全投影进入控制台；只有显式 `--access-log` 时才恢复其 INFO/access 输出。两个文件
继续使用 UTC `key=value` 格式并保留完整可用的
`run_id`、`turn_id`、`trace_id`，用于从入口 lifecycle 串联到 runtime trace。
两个文件均通过标准库 `RotatingFileHandler` 轮转，
单文件上限 5 MiB，保留 3 个备份；重复配置或 reload 不得重复安装 handler。
launcher 通过显式进程环境把 level/path 传给 `create_app()`，因此 reload 后的实际
server 子进程会重新执行同一幂等配置。文件目录或 handler 打开失败时保留 Combined
console 并 fail-open，不得阻止应用启动。

Gateway 日志来自 `GatewayLifecycleEvent` 的 fail-open sink，覆盖 session、queue、
admission、run、cancel、interrupt 和 terminal 边界。它保留 `run_id` / `turn_id`，
但只记录 allowlist 内的状态、计数、reason/source 等 prompt-safe 字段；`user_id` 与
`session_id` 使用稳定短摘要，不记录用户文本。Agent-Service 连接日志同样只记录
query key、session 摘要和聚合计数，不记录 query value、原始 session ID 或媒体内容。

Runtime 日志由 `OperationalTraceLogStore` 作为 server `CompositeTraceStore` 的只写
secondary 生成。输入先经过现有 trace redaction，再只投影 canonical event、status、
tool/provider/model、latency、error code 与关联 ID；prompt、response、memory、
`attributes` 整体、input/output summary 和 Provider raw payload 均不进入文本日志。
该视图只用于实时开发排障，`.data/graph_trace.jsonl`、trace query API 与
`scripts/trace_view.py` 仍是机器查询和调试重建权威。

`scripts/run_server.py` 提供 `--console-level`、`--file-log-level`、
`--console-mode {concise,verbose}` 与 `--log-dir PATH`，默认分别为 `INFO`、`DEBUG`、
`concise` 和 `.data/logs`。需要临时逐事件观察时使用 `--console-mode verbose`；旧
`--log-level` 仍作为同时覆盖 console/file level 的兼容 shorthand。共享 PyCharm 配置
`.run/Assistant Server.run.xml` 使用 `hello_agent` 解释器和 mock Provider 启动：
Run console 是 Combined 页签，Gateway 与 AgentRuntime 页签分别跟随上述两个文件。
`.run/Gateway Debug Turn.run.xml` 使用固定 `pycharm-debug-session` 发起一轮 Gateway
调试请求；`.run/Trace Last.run.xml` 一次性展示同一 session 在
`.data/graph_trace.jsonl` 中最后活跃的 run；`.run/Trace Follow.run.xml` 常驻跟随同一
trace 文件，适合作为第三个开发观察页签。
`.run/Trace Full.run.xml` 连接本地 server，按 Conversation、Timeline、ReAct detail
三层查看最后一轮；`.run/Trace Full Follow.run.xml` 则常驻跟随同一 session 的完整三层视图。
Conversation 层要求 server 已显式启用 `--allow-local-trace-content`。
这些配置不启用 `--allow-local-trace-content`，也不保存密钥或 `.env` 路径。

## Realtime Video Observation

Realtime video observation remains visible through the governed background
`video_understanding` execution and the redacted context projection consumed by
the foreground model. These are distinct boundaries: Qwen still runs behind
`ActionValidator` and `ToolExecutor`, while the Agent-Service DeepSeek tool
catalog does not expose `video_understanding`. Structured diagnostics use these
prompt-safe sources:

- `background_keyframe_observation`: a selected keyframe was analyzed by the
  per-connection observer through `ActionValidator` and `ToolExecutor`;
- `realtime_video_context`: the foreground model consumed the latest rolling
  snapshot on its first context build, with no query-time visual Provider call;
- `rolling_video_memory`: a non-Agent-Service explicit tool query reused the
  latest healthy semantic snapshot;
- `recent_frame_fallback`: semantic memory was absent, not ready, or latest
  failed, so the ordinary recent-frame Provider path ran.

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
foreground tool calls fall back to tool-result projection. If
`recent_frame_fallback` performs a query-time Provider call,
that work is inside `tool_execute[video_understanding]` and can become the turn
bottleneck.

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
| `TraceStore` / `TraceQueryService` | services | Redacted run and trace summaries for `/runs/{run_id}`, `/traces/{trace_id}`, and tool-call debug views. |
| Operational text logs | services / gateway | Combined console plus isolated rotating Gateway and AgentRuntime developer views; never replace canonical trace JSONL. |
| `react_steps` / `decision_trace` | API response metadata | Compact per-response ReAct timeline for developer UI and CLI output. |
| `RunHistoryStore` / `ToolHistoryStore` / `SessionStore` | services | Local JSONL/session indexes and lifecycle ledgers. |
| Gateway frames | gateway | Realtime wire lifecycle: `run.started`, `event.progress`, `stream.chunk`, `run.end`, `run.cancel`, call hangup, and config updates. |

These surfaces may expose the same fact at different fidelity. The canonical
source for debug reconstruction should be the redacted trace timeline. API
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
event_id
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

## Canonical Timeline

Use stable dotted event names for the internal trace timeline. Existing
`AgentEvent`, Gateway frame names, and response metadata can keep their current
public names, but they should map to this vocabulary.

| canonical event | meaning |
| --- | --- |
| `run.started` | A user request has entered the assistant runtime. |
| `gateway.turn.started` | Gateway accepted a normalized user turn. |
| `memory.load.started` / `memory.load.finished` | Memory context load lifecycle. |
| `context.build.started` / `context.build.finished` | Prompt/native context pack construction and budget report. |
| `llm.chat.started` / `llm.chat.finished` | One provider chat call, including direct-answer and native tool-call responses. |
| `react.decision` | Assistant selected final answer, follow-up, tool call, or plan transition. |
| `action.validation.started` / `action.validation.finished` | Local validation before tool execution. |
| `tool.started` | Tool execution lifecycle began through `ToolExecutor`. |
| `tool.finished` | Tool returned successfully, including duplicate suppression or pending confirmation result. |
| `tool.failed` | Tool failed, budget was blocked, or cancellation interrupted execution. |
| `tool.observation` | Tool result was converted into assistant-facing observation data. |
| `loop_guard.triggered` | ReAct guard stopped or redirected the loop. |
| `response.delta` | User-visible response text chunk was emitted. |
| `response.final` | Final response was set. |
| `memory.save.started` / `memory.save.finished` | Post-run memory save or promotion lifecycle. |
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
Memory lifecycle calls are wrapped at the runtime and graph memory boundaries:
`memory.load.started` / `memory.load.finished` summarize retrieval/injection
counts, token budget fields, retrieval version, and injected memory IDs;
`memory.save.started` / `memory.save.finished` summarize promotion/save
decisions, skipped reasons, and written IDs. These events must not include
memory summaries, rendered memory context, candidate content, or raw user text.
Final response tracing emits `response.final` with only prompt-safe response
shape data such as message presence, character count, output-ref count, response
data keys, status, and error count. It must not include the response text.

## Span Model

Trace records should move toward a span-plus-event model:

```text
run
  gateway.turn
  memory.load
  context.build
  llm.chat(iteration=1)
  react.decision(iteration=1)
  action.validation
  tool.execute(product_search)
    provider.call(product_search_provider)
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

`attributes` must be prompt-safe and redacted. It can include tool names,
provider names, model names, token counts, latency, retry count, budget summary,
risk gate summary, side-effect level, confirmation state, output references,
context budget summaries, and compact error codes.

## Developer UX

The harness should optimize for a developer debugging locally.

Current query surfaces:

```bash
GET /runs/{run_id}
GET /traces/{trace_id}
GET /runs/{run_id}/tool-calls
GET /traces/{trace_id}/conversation  # explicit loopback debug only
```

Local CLI:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py last
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py last --errors
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py last --follow
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py last --sections timeline,react
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py last --trace-path .data/graph_trace.jsonl --server http://127.0.0.1:8000 --session-id pycharm-debug-session --sections conversation,timeline,react --errors --follow
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id>
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id> --errors
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id> --json
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id> --server http://127.0.0.1:8000
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py --trace-path .data/graph_trace.jsonl
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py --json
```

`last`、`latest` 和 `@last` 都解析为本地 JSONL 文件中最后活跃的 run，避免从
控制台复制 `run_id` / `trace_id` 才能打开详情。`--session-id` 会先过滤本地
JSONL，再解析 `last` 或驱动 `--follow`，避免多个调试客户端共用同一个 trace 文件时看串
session；客户端和 trace viewer 必须使用同一个 `session_id`。如果不传
`--session-id`，`last` 仍表示全局最后活跃的 run。

server 参数和 trace viewer 参数分开理解：`scripts/run_server.py` 的参数负责启动
runtime、mock provider、日志和 `--allow-local-trace-content` 内容开关；
`scripts/trace_view.py` 的 `--trace-path`、`--server`、`--sections`、`--follow` 和
`--session-id` 只负责查询与展示。`--follow --server` 的数据流是：本地
`.data/graph_trace.jsonl` 发现当前 session 最新 trace 或变化，再用 `trace_id` 向
loopback server 拉 `/traces/{trace_id}`；包含 `conversation` 时，再拉
`/traces/{trace_id}/conversation`。server 查不到 trace 时降级使用本地 summary；
conversation 查不到时标记 unavailable，仍继续输出 Timeline 和 ReAct detail。

`--sections` 控制输出层级：`conversation` 需要 `--server` 且 server 已用
`--allow-local-trace-content` 启动；`timeline` 是默认事件线；`react` 展示
prompt-safe 的 LLM 决策、validator、tool 调用、耗时、错误和恢复动作证据。server-backed
view 会在事件 timeline 前渲染 `turn_latency`、stage rows、bottleneck、ACK state 和
consumed-video diagnostics。Conversation text 不会写入 trace events 或 JSONL。

推荐 PyCharm 本地流程：

1. 运行 `.run/Assistant Server.run.xml`。需要 Conversation 层时，临时给 server 参数增加
   `--allow-local-trace-content`。
2. 运行 `.run/Gateway Debug Turn.run.xml`，它固定使用
   `--session-id pycharm-debug-session`。
3. 运行 `.run/Trace Full Follow.run.xml`，它用同一个 session 常驻输出
   Conversation -> Timeline -> ReAct detail。

共享 `.run` 配置保持 `http://127.0.0.1:8000`。实际通话测试如果本机 server 跑在
`8089`，复制对应 PyCharm 配置到个人配置后，把 `--server http://127.0.0.1:8000`
改成 `--server http://127.0.0.1:8089`，不要把 `8089` 写回共享配置。

For a one-off local debug view, restart the server with the explicit gate and
request only the matching turn:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --provider mock --image-provider mock --allow-local-trace-content
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py last \
  --trace-path .data/graph_trace.jsonl \
  --server http://127.0.0.1:8000 \
  --session-id pycharm-debug-session \
  --sections conversation,timeline,react \
  --errors
```

`--include-conversation` remains as a compatibility shortcut for adding the
conversation section. Conversation lookup rejects non-loopback server URLs. The
server endpoint is disabled by default, checks the socket peer, joins the
existing `ConversationStore` by trace identity, returns only the current
user/final-assistant pair, and clips each side to 1000 Unicode characters with
an explicit truncation marker. Do not enable this gate on a shared or production
process.

Human-readable output should show:

```text
run run_xxx trace trace_xxx status=failed duration=1832ms
01  run.started                         0ms
02  context.build.finished             12ms budget=42%
03  llm.chat.finished                  820ms provider=mock model=...
04  react.decision                       tool_call product_search
05  action.validation.finished           accepted
06  tool.started                         product_search risk=external_read
07  tool.failed                          provider_timeout retry=1
08  tool.observation                     failed recovery=retry_or_report
09  run.failed                           PROVIDER_TIMEOUT
```

Developer output should group errors first when requested, then show the full
timeline. The default view should be short enough to paste into an issue or a
handoff note. Metrics output should answer aggregate health questions without
opening individual traces first.

## Metrics

The first metrics layer should remain small and derived from trace/events where
possible:

| area | metrics |
| --- | --- |
| Run | count, success rate, failure rate, cancel rate, duration p50/p95. |
| Tool | call count by tool, latency, failure rate, retry count, risk gate decisions, confirmation-required count. |
| LLM | chat call count, latency, token usage, native tool-call rate, direct-answer rate, provider error count. |
| Context | total chars/tokens, budget ratio, compaction triggered count, overflow retry count. |
| Gateway/realtime | active runs, interrupt count, cancel source, hangup cancellation, deadline expiry. |
| Memory | retrieval count, save candidate count, confirmed/rejected count, promotion counters. |

Do not add high-cardinality labels such as raw prompts, raw queries, full URLs,
full memory text, full provider errors, or media payloads.

The local metrics command currently exposes this first layer:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py
```

It reads the redacted JSONL trace store, supports optional `--user-id` and
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

`assistant_agent.services.trajectory_debug` provides the Phase 5 local
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

## Redaction Rules

Trace and monitoring records must not include:

- API keys, tokens, Authorization headers, cookies, or secret-like strings.
- Raw provider payloads or raw provider responses.
- Full prompts, system messages, or rendered context bodies.
- Hidden chain-of-thought or internal reasoning fields.
- Raw memory content beyond redacted summaries and counters.
- Base64, inline media payloads, full command outputs, or large binary/text blobs.
- Real user data dumps.

Safe trace data includes:

- IDs, statuses, event names, error codes, recoverability, and component names.
- Tool names and prompt-safe input/output summaries.
- Provider/model names when not secret.
- Latency, token counts, retry count, budget summaries, side-effect/risk gate summaries.
- Output references and artifact IDs.
- Redacted context budget and compaction summaries.

Redaction is part of the harness contract. Tests should fail if trace/API output
contains obvious secret or raw payload keys.

## Harness Invariants

Regression tests should enforce these invariants:

- Every `run.started` has exactly one terminal `run.completed`, `run.failed`, or
  `run.cancelled`.
- Every `tool.started` has a matching `tool.finished` or `tool.failed`.
- Every `tool.observation` references a prior tool call or a validation rejection.
- Native provider runtime and mock/offline ReAct runtime both emit
  `react.decision` and terminal run events.
- Successful native provider runtime and mock/offline ReAct runtime both emit
  `response.final` before the terminal run event.
- Native provider runtime emits skipped `memory.save.started` /
  `memory.save.finished` events when automatic task-summary memory is delegated
  to explicit LLM `memory_save` tool calls.
- A validation rejection never enters `ToolExecutor`.
- A failed tool carries error code, source, recovery action, and redacted message.
- Cancel, interrupt, timeout, and hangup traces include their source.
- `/runs/{run_id}` and `/traces/{trace_id}` expose only redacted summaries.
- No public trace, API response, or realtime trace event exposes hidden reasoning.
- Local hook auditing can use `TraceInvariantObserver` to check the first local
  invariant set in-process: run terminal events, tool terminal events,
  `tool.observation` provenance, failed-tool error detail, and hook dispatch
  error redaction.
- Phase 0 trace invariant gate tests live in
  `tests/scopes/runtime/test_observability_harness.py` and
  `tests/scopes/runtime/test_hook_invariants.py`.
- Current harness development should stop after these invariant tests pass.
  Future work should be driven by a concrete debugging gap rather than adding
  more event types, dashboards, exporters, or debug endpoints preemptively.

## Phase Plan

### Phase 1: Align Trace Semantics

- Add canonical event names to trace summaries without breaking existing public
  response fields.
- Make native provider runtime write `llm.chat`, `react.decision`,
  `action.validation`, `tool.observation`, and terminal run events to
  `TraceStore`.
- Keep existing LangGraph node trace for mock/offline debugging, but map it into
  the canonical timeline.
- Add invariant tests for native and mock/offline runtime paths.

### Phase 2: Developer Trace Viewer

- Add `scripts/trace_view.py`.
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
- Export should be disabled by default and must use the same redaction boundary.

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
- Force explicitly requested allowlisted suites into a sanitized `offline_eval`
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

# Runtime Event Stream Architecture

Last updated: 2026-08-13

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Assistant loop 与 Provider/runtime event stream 的当前权威 |
| Owns | `LLMEvent`、`AgentEvent`、`AgentRunStream`、stream/result、thread bridge、取消与终态 |
| Does not own | Gateway frame/session、Tool 治理、trace schema、prompt/context 预算 |
| 源码与 schema 入口 | `src/assistant_agent/runtime/`、`src/assistant_agent/providers/llm_events.py` |
| 验证入口 | `docs/authority.toml` 中 `runtime-event-stream.verification` |
| 相邻 authority | Gateway 见 [`gateway-architecture.md`](gateway-architecture.md)；Tool 见 [`tool-calling-architecture.md`](tool-calling-architecture.md) |

This document is the current authority for provider and assistant runtime
streaming in `assistant_agent`. It defines the event contracts, stream/result
separation, thread bridge, cancellation limits, compatibility boundaries, and
source ownership that are implemented today. Gateway session and wire-frame
lifecycle remain authoritative in `docs/gateway-architecture.md`.

## Scope And Invariants

The streaming stack has five distinct contracts:

```text
vendor provider chunks
  -> provider adapter -> LLMEvent
  -> stable compiled AssistantTurnGraph astream(v2)
  -> runtime lifecycle / ProductEventProjector -> AgentEvent
  -> AgentRunStream / shared assistant service stream
  -> RealtimeAgentEvent
  -> Gateway frame
```

- `LLMEvent` is provider-neutral and internal to the chat/provider boundary.
- `AgentEvent` describes assistant runtime progress and lifecycle.
- `RealtimeAgentEvent` is the thin realtime backend boundary.
- Gateway frames describe session, run, delivery, cancel, interrupt, and
  transport lifecycle.
- `TraceEvent` is the independent observability projection consumed by the
  local trace store and LangSmith native tracing; it is not part of the
  `AgentRunStream`.
- Vendor chunks, SDK objects, prompts, credentials, and raw provider responses
  do not cross the provider adapter boundary.
- Gateway, UI, TTS, and public API consumers do not consume `LLMEvent`.
- Provider streaming never bypasses tool governance. Final native tool calls
  still enter `ActionValidator -> ToolExecutor -> ToolRegistry`.

`AgentEvent` and `TraceEvent` remain separate public contracts because delivery
and observability have different payload, retention, and redaction rules. Shared
run, governed-tool, and ReAct decision/observation lifecycle facts are published
through `RuntimeEventPublisher`, which creates both projections with the same
occurrence timestamp and correlation identity. Runtime and Tool code must not
construct a second lifecycle projection by hand after publishing the fact.

standard 模式只有一套主运行图：每个 `AgentGraphRuntime` 只编译一次并稳定持有
`AssistantTurnGraph`，在 `assistant -> execute_tool -> assistant`、
`assistant -> await_input -> execute_tool|assistant` 与
`assistant -> compose_response` 之间按真实 conditional edge 循环、等待或收口。Provider adapter、
Tool executor、event sink 与 cancel token 通过 LangGraph runtime context 注入，不存入 graph state。
生产 Agent-Service 与默认 HTTP Gateway 消费 `astream(v2)` 的 native async Runtime 路径；
逻辑 `ProductEventProjector` 只把已发生的 Runtime 事实投影为现有 `AgentEvent`/
`RealtimeAgentEvent`，不决定 graph 路由。`GraphStreamPart`、namespace、checkpoint、task 与完整
state 不进入产品协议。

`AssistantTurnGraph` 的 checkpoint boundary 是版本化、严格 JSON 的 `AssistantTurnState`：只保存 request、
run、Tool trajectory、prompt-safe observation、稳定 context/capability/artifact reference、profile、phase、
counter、pending Tool call/interrupt 和 final response 等恢复事实。`AgentState`、Provider/Tool client、Registry、
Executor、service/store、callback、cancel token、媒体正文、任意 metadata/data 都不进入 checkpoint；节点仅在
一次调用期间从 `GraphRuntimeContext` hydrate 运行对象，返回前重新投影 strict state。同一 conversation 的新
turn 会显式覆盖所有 run-scoped channel，不能依靠 LangGraph merge 偶然清除上轮事实。

同一 node/edge 实现还编译为 `standard`、`planner`、`worker`、`verifier` 四个稳定 profile child。
profile 只能来自受信调用参数或父图 assignment，并同时收窄 Provider Tool schema、RunToolCatalog、Validator
可执行集合和预算，不能从用户文本推断。standalone root 可绑定 saver；profile child 自身不绑定 saver，作为
subgraph 时继承父图 checkpointer/namespace。父子图通过窄 input/output adapter 交换 assignment reference、
objective、constraint、capability reference、response、Tool trajectory 和 artifact reference，不传递父图整份
state 或动态 task UUID。

Graph 身份语义为 stable conversation `thread_id` 加每次调用或 resume 的新 `run_id`。进程拥有的
`AssistantRuntimeApp` 只解析并持有一个 `AgentGraphRuntime`；owner-bound history 由 App/Host 直接委托
同一个 compiled graph，Replay 与 Fork 通过需要 continuation service preparation 的 Runtime 入口复用该
graph、checkpointer、claim store 和受治理 service。continuation preparation
从已验证 checkpoint 重建 fresh invocation-local `AgentState`/`GraphRuntimeContext`，不调用 Memory
`open_session`、`prepare_context` 或 `ingest_turn`：普通 turn 不变，resume 只在成功终态 ingestion 一次，
Replay/Fork 不 ingestion。非 Memory context ref 必须由当前 owner 重建并严格匹配；Memory ref 只能通过
Host 的 owner/origin/ref-bound active frozen context attach 契约解析，不读取底层 store、不回退当前 baseline；
origin 已 terminal/release 或进程重启后缺少 exact frozen context 时 fail closed。checkpoint state schema v3
显式持久化 logical Memory origin 与 `product_turn|time_travel` provenance，旧 v2 continuation fail closed。该内部
facade 不进入 `AssistantRunService`、HTTP/WebSocket、Gateway 或 Agent-Service wire，也不产生 selector、
checkpoint/native task/interrupt ID 产品事件。

compiled app
显式接收 checkpointer；绑定 saver 时，内部 `AssistantTurnGraphApp` 以 native `aget_state`/
`aget_state_history` 读取 checkpoint 与 state history，并以 `interrupt()`/
`Command(resume=...)` 在同一 thread 恢复。`GraphStreamResult` 的 completed/interrupted 分类取自执行后的
native snapshot（pending tasks/next 与稳定 Interrupt id），不从 root `values` 或 stream namespace 猜测。
trusted interrupt request 只允许结构化 `approval|input`，并绑定当前 pending Provider Tool call 或当前
assistant turn；普通 write/dangerous category、用户文本和任意 metadata 不自动触发 HITL。该能力当前只供
内部 compiled graph / Runtime 与后续父图使用；Agent-Service、Gateway、HTTP 和媒体 wire 不接收 resume，
也不投影 `waiting_user`。未绑定 saver 的显式 offline/test graph 不宣称恢复能力。
checkpointer backend 只允许显式 `none|memory|sqlite`。`none` 不保存执行位置；官方 `InMemorySaver`
仅用于本地/离线 Graph API 验证，不声明跨 host durability。`sqlite` 由进程级
`AsyncCheckpointerOwner` 唯一创建、`setup` 并关闭官方 `AsyncSqliteSaver`，要求部署方提供绝对
`LANGGRAPH_CHECKPOINT_PATH`；缺依赖、路径、async owner 或无法打开时 fail closed，不允许自研 saver 或
静默 memory fallback。该 owner 同时持有独立 sibling 业务 SQLite claim store，但 invocation claim 绝不写入
checkpoint channel。`RuntimeHost.aopen/aclose` 固定先开 saver/claim 再构造 Runtime；async-owned host
不暴露裸 Runtime 或同步 graph 入口，invoke/resume/history/Replay/Fork/stream/thread delete 只能走受
active lease 保护的 facade。关闭时先拒绝新 invocation 并排空已有 consumer，再按 Runtime、trace store、
claim connection、saver 的顺序释放。Gate P1 已用 first host 完全关闭后创建
fresh second host，验证同一 thread 的 interrupt/resume、history、Replay、Fork 与 run claim 拒绝；Task 9
production cutover 前，现有同步 Agent-Service/API composition root 仍不启用 `sqlite`。Memory Plugin 的
active frozen context 仍是进程内治理事实：checkpoint 携带 Memory ref 而 fresh host 缺 exact frozen context 时
继续按既有契约 fail closed，持久 checkpointer 不冒充长期 Memory。

`AssistantTurnGraphApp.invoke/astream/arun/aresume` 在进入 tracing、checkpoint preflight 或 native graph
之前统一原子 claim `(owner, thread_id, run_id)`；相同 invocation token 的 pre-native 重试幂等放行，不同 token
复用同一 run 必须以结构化 `GraphExecutionError` 拒绝。claim 在 native 边界原子推进
`pre_native -> native_started`，只允许一个并发 caller 开始执行；观察到完整终态后推进 `terminal`，提前关闭
stream 或执行异常则保持 `native_started`，两者都不得再次执行。claim 不随无 saver 终态、校验失败或执行异常自动释放，
因此 invalid resume 也会占用该 run identity：调用方只能使用相同 token 和 run 修正重试，不能换 token 复用。
`memory` backend 使用有界进程内 store，容量耗尽时 fail closed；`sqlite` backend 在独立业务表上以
`PRIMARY KEY (owner_digest, thread_id, run_id)`、事务与 phase CAS 持久化 claim，并在独立表持久化 thread
tombstone，fresh host 不能重新执行已 claim 的 run。只有 retention owner 在销毁整个 thread/checkpoint
生命周期后显式调用
host retention owner 通过 `adelete_thread` 先删除 native checkpointer thread，确认成功后才调用
claim store 完成该 thread 的全部 claim 删除；删除开始前 store 原子设置 thread tombstone，阻止等待
checkpointer 期间的新 claim、已取得 pre-native claim 的 `begin_native` 及 direct graph gate。若 thread 存在
`native_started` claim，删除以 `graph_thread_active` fail closed；只有全部 invocation 已 terminal 或尚未开始时
才能冻结。checkpointer 删除失败时撤销 tombstone 但保留原 claim；成功时
原子删除 claim 与 tombstone。底层 `delete_thread(owner, thread_id)` 只供已经协调完外部 retention 的 owner 使用。
当前业务
session deletion 尚未拥有完整的多 agent/checkpointer retention 协调，因此不隐式调用该 host API，后续 persistent
composition root 必须显式接线。thread 已销毁后，确定性 thread key 即使再次出现
也属于新的 retention 生命周期，可以重新 claim；调用方不得把 `delete_thread` 当作单 run retry 接口。
M3 已在离线路径编译 `DurableWorkflowGraph`，并以 native v2
`updates/custom/tasks/checkpoints` stream、subgraph namespace、`Send` super-step、父图 interrupt snapshot、
同 thread 新 run resume 和最终 snapshot 判定验证 Graph API 事实。该 app 目前只由 TDD probe 与 LangSmith
workflow regression offline target 使用，尚未由 Agent-Service/API 的 production composition root 持有；
因此它不改变下文所述当前 Deep Research work-item stream、lease 或恢复边界。
当前 Durable Workflow graph 没有跨节点 namespace/key/value consumer，因此不装配 LangGraph Store；短期执行
记忆只属于 strict state 与 checkpointer，长期记忆仍只通过 `MemoryPluginHost` 生命周期治理，正文不进入
checkpoint。Assistant graph 的内部 replay/fork 在进入 native stream 或创建 branch 前，只检查所选 checkpoint
下一步会执行的 pending Tool：read 可重放；write/dangerous 必须保留 `toolop:` stable scope 并复用业务 operation
ledger。该 operation 已处于 `reserved|invoking|outcome_unknown` 时 fail closed；无关 operation 不参与分类，
pending call 同时保存 effect category；当前 Registry category 与 checkpoint 不一致、stable scope 无法由
turn origin/iteration/ordinal/tool/arguments 重算，或 ledger identity/bound-input digest 不一致时均 fail closed。
Registry generation 是稳定装配清单与 ToolSpec 的 digest；time travel 要求 checkpoint generation 和每个 pending
Tool contract digest 都与当前受信 Registry 一致。pending effect 另保存完整 execution contract digest（含完整
input schema、runtime input bindings 与 hidden fields）和 checkpoint-time bound-input digest；当前 Runtime 无法
重建完全相同输入时，即使 ledger 尚无 row 也禁止重放。新增安全字段保持旧 state schema 的普通 resume 兼容，但缺少
这些字段的旧 `execute_tool` checkpoint 不可 replay/fork，按 `graph_time_travel_effect_forbidden` fail closed。
产品事件仍只观察已经发生的 native fact，不能作为重放判据或副作用事实源。
仓库不再保留 conditional graph、rule intent/router/planner 或可切换它们的 `AGENT_GRAPH_MODE`；
`UserRequest` 也不再接受 `execution_strategy=plan_and_solve`。当前仍存在的
`task_execution_mode` 是工具/持久执行的结构化治理事实，不是第二张 Agent graph 的选择器。

Durable Workflow 的 Plan、work-item run、返工和 terminal 生命周期不属于单个 bounded Assistant run，因此其
事实源是已提交的 `WorkflowEvent`，而不是把它们复制为 `TraceEvent`。Deep Research 是一个逻辑
Plan-and-Execute runtime：入口把当前 canonical trace ID 持久化到 Workflow，首个 durable planner run 调用
主 Agent planner 生成结构化 DAG，后续 work item 再由可替换的 worker Agent 执行。planner/worker 每次
调用仍产生自己的 canonical `AgentEvent/TraceEvent`，并保留 trace/run/attempt 身份。LangSmith 通过
production compiled graph 的 native callback 观察 planner/worker 的 actual tree；Runtime 不注入外部
trace identity，也不重建 Workflow shadow tree。这不会改写 canonical event 身份，也不会把 Workflow
状态机塞进单次 Assistant loop。

The projections are not one-to-one. Delivery-only facts such as committed text
deltas remain `AgentEvent` only, while LLM, context, memory, and graph-node
diagnostics remain `TraceEvent` only. `response.final` is the response-composition
span; `final_response` is the delivery terminal and therefore is emitted from
the run-terminal fact rather than treated as the same event. Run terminal trace
projection is recorded before `assistant.turn.summary`, while terminal delivery
is emitted after trace finalization.

Context evidence also has a single owner: `context.build` carries
`context_report_v2`, while the local `llm.chat` content overlay carries the exact
Provider input and its generation span carries the equivalent formatted projection.
`assistant.output` records only the normalized decision and does not duplicate
either payload. The `agent.runtime` root may retain the scalar
`context_peak_ratio` as a turn-level diagnostic, but not the full context report.

## Provider Stream Boundary

`AsyncStreamingChatAdapter.stream_chat()` is an optional additive interface.
Implementations normalize vendor chunks into these `LLMEvent` variants:

| event | purpose | terminal |
| --- | --- | --- |
| `token_delta` | prompt-safe response text progress | no |
| `reasoning_delta` | hidden reasoning progress; accumulated for provider continuity only | no |
| `tool_call_delta` | accumulated native tool-call name and arguments | no |
| `completed` | finish reason, usage, and stream completion | yes |
| `error` | prompt-safe provider failure | yes |

`LLMEventAccumulator` reconstructs response text, reasoning content, tool calls, finish reason,
usage, provider, and model into the existing terminal `ChatResult` contract.
Tool-call argument deltas are not exposed as user-visible response events.
Provider errors become structured `ChatResult.errors`; cancellation exceptions
remain cancellation signals rather than provider errors.
If a provider stream ends with `completed` but no text, tool calls, or refusal,
the runner normalizes it to `provider_empty_response` so sync and streaming chat
paths share the same empty-output contract.

`reasoning_delta` never maps to `AgentEvent(type="response_delta")` and never
enters the public answer or conversation history. Runtime routing uses the
normalized Provider result directly: non-empty `tool_calls` enter tool
governance; otherwise refusal is returned as refusal text, `finish_reason=length`
is treated as an incomplete response, and non-empty `response_text` becomes the
terminal answer. 前台 chat Provider 的 `token_delta` 到达后立即映射为公开
`AgentEvent(type="response_delta")`，不再等待当前 Provider turn 的终态；同一 turn
后来出现的 `tool_call_delta` 仍只在内部累积，参数完整后进入工具治理。因而 content 与
tool call 可以共存：已经交付的 text 是不可撤回的 provisional 文本，工具执行后下一轮
Provider text 继续追加到用户流。若工具前导文本与下一轮可见正文的交付边界两侧都没有
换行，Runtime 会在下一轮首个 text delta 前补一个 `\n`；任一侧已有 `\n` 或 `\r`
时不重复补。该分隔符只属于 delivery projection，conversation history、memory 和
`AgentResponse.message` 仍只保存 Runtime 归一化的终态回答，不机械拼接这些
provisional 前导文本。

For the main foreground chat LLM only, `provider_timeout` and
`provider_empty_response` with no usable text/tool/refusal are treated as a
recoverable no-answer condition. The runtime records the structured provider
diagnostic in state metadata, response data, and trace events, but completes the
run with an honest user-visible retry prompt instead of emitting `task_failed`:
`provider_timeout` reports `抱歉，刚才主模型没有及时响应，请再说一遍。`, and
`provider_empty_response` reports `抱歉，刚才主模型返回为空，请再说一遍。`.
Tool providers, vision/search providers, durable-task provider calls, and
cancellation paths do not use this fallback.

assistant loop 在每次 Tool 执行后保留统一的 runtime result hook。当前 `load_skill` 成功结果通过该
hook 生成 `CapabilityGrant`，先更新当前 `AgentState`，再以 user/agent/session identity 幂等持久化；
因此同一 run 的下一次 Provider 调用即可获得扩展后的 Tool catalog，下一 turn 则在创建 state 后从
SessionStore 恢复。失败的 Tool、调用方 metadata 或模型输出文本都不能生成 grant。恢复时 Runtime
重新读取当前 `skill.toml` 并重建 Tool 名称，已删除、禁用或改为不兼容 activation 的 Skill 不再生效。
context activation 也走相同 grant/session 模型，但来源只能是结构化 entry/media/env 资格事实。
grant 是 session store 的内部恢复字段，不进入公开 Session API 投影；持久化失败会记录结构化 runtime
error，不会把已成功的 ToolResult 改写为 Tool 失败。同一进程内并行 run 的 session grant 更新由
store 实例原子串行化，避免 read-modify-write 丢失；JSONL backend 不在此基础上宣称跨进程事务能力。

Foreground provider turns are consumed inside the shared LangGraph assistant
loop. 配置为 `qwen` provider 的主 Agent 默认使用百炼 DashScope Generation API 和
Provider-native 联网；`QWEN_CHAT_API_PROTOCOL=openai_compatible` 只作为显式兼容回退。
普通 `assistant_mode=standard` 保持既有
`enable_search=true`、`enable_thinking=false`、`search_strategy=turbo`、
`forced_search=false`、`enable_search_extension=true`、`enable_source=true`、
`enable_citation=true`、`citation_format=[<number>]`、`freshness=7`；显式
`deep_research` Workflow work item 使用 run-scoped `provider_search_profile=deep_research`，改为
`enable_thinking=true`、`search_strategy=max`、`forced_search=true`、
`enable_search_extension=true`，保留 source/citation 契约且不设置全局 freshness。前台模式提交仍使用
普通搜索配置，避免在创建 Workflow 前进行一次无意义的研究搜索。DashScope adapter 在启用
`MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING` 时使用百炼原生 HTTP SSE：请求携带
`X-DashScope-SSE: enable` 且设置 `incremental_output=true`，每个增量立即归一化为
`LLMEvent`；未启用时仍通过同步 HTTP 聚合一次完整响应。profile 来自结构化请求和可信 Workflow assignment，不根据
自然语言推断。普通请求的默认输出预算为 1024 token；可信 `deep_research` Workflow work item 默认
使用独立的 8192 token 预算，两者分别可由 `MULTIMODAL_AGENT_CHAT_MAX_TOKENS` 和
`MULTIMODAL_AGENT_DEEP_RESEARCH_MAX_TOKENS` 覆盖。
adapter 将 DashScope `search_info.search_results` 归一化为 `ChatResult.search_sources`，只接受
HTTP(S) URL、按 URL 去重并在结构化结果中最多保留 20 条。Runtime 原样保留 Provider 正文中的
`[1]` 或 `[ref_1]`，不追加底部来源列表，也不把角标改写成 Markdown 链接。角标语义完全沿用
Provider 在 `enable_citation=true` 下生成的内容，Runtime 不自行插入或伪造引用；
这证明 Provider 返回了哪些来源，但不把引用覆盖率或网页内容正确性扩大声明为已验证事实。显式
OpenAI-compatible 回退不提供该结构化来源契约。Provider 非工具终态由 Runtime 的唯一 citation
解析器把正文中实际出现且能匹配安全来源的 `[n]` / `[ref_n]` 投影成
`UrlCitationAnnotation(type="url_citation")`；正文保持不变，annotation 使用 Unicode code point
半开区间并携带 `source_id/title/url`。重复角标产生多个 occurrence、复用同一 `source_id`，未引用来源、
无匹配角标、已是 Markdown link 的角标和非 HTTP(S) URL 不进入产品响应。annotations 随
`AssistantTextOutput -> AgentResponse -> AgentRunResponse` 进入 HTTP `/agent/run` 终态，不进入
conversation history、TTS 或 token delta。可点击样式和跳转仍由客户端负责；CLI 只能验证结构化映射，
不能替代点击交互验收。
`MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING` 只控制 Runtime 是否使用 async-native stream
consumer；DashScope 的流式终态继续保留 `request_id`、usage 与结构化搜索来源，传输模式记为
`dashscope_sse`，但不把 vendor 原始 envelope 暴露给客户端。Judge 等显式直接构造且未开启
`native_web_search` 的辅助 adapter 保持独立的非联网、非流式策略。Other providers remain opt-in through
`ProviderConfig.native_provider_streaming`. When enabled and the adapter exposes
`stream_chat()`, `ProviderStreamingTurnRunner` consumes the async stream for one
runtime turn. Visible token deltas pass through the existing stream callback and
`llm_event_mapping` to become `AgentEvent(type="response_delta")`. When the flag
is disabled or the adapter is sync-only, the runtime continues to call
`ChatAdapter.chat()`.

Every foreground assistant-loop Provider turn emits a paired
`llm.chat.started` / `llm.chat.finished` span. The finished event records bounded
provider/model labels, iteration, a derived `result_kind`, tool-call count, Provider-reported
latency, wall latency and normalized token usage; it never records prompt or
response content. 它还记录 route、runtime action、transport mode 与 delta count 等安全摘要，
以及 `search_performed/search_source_count`；canonical event 不保存来源标题或 URL。`result_kind` is computed at the runtime/observability boundary as
`error | tool_call | refusal | truncated | text | empty`; it is not stored in
`ChatResult` and is not a Provider protocol field. Agent-Service latency summaries use wall latency as the
critical-path `llm_chat[n]` duration and keep Provider latency as a nested
diagnostic.
启用 local trace content 后，进程内 debug overlay 保存发送给 Provider 的请求形状与归一化
`ChatResult`；额外设置
`MULTIMODAL_AGENT_LOCAL_PROVIDER_PROTOCOL_CAPTURE=1` 后，还保存原始 content、原始工具参数字符串、
finish reason、usage、结构化 search sources 与流式事件计数组成的协议语义快照。overlay 不进入
canonical ledger，也不改变 SDK 调用的 messages/tools 语义、生成参数或 Provider 返回，
并按上述 Provider-neutral 形状支持 formatted renderer，不为展示虚构 message role；
finish reason 保留在 trace/协议快照，
usage、route 与 transport 保留在诊断字段，都不拼接到 output 文本。默认 trace event 保持安全摘要；
本地 `.data/trace_ledger/YYYY-MM-DD.jsonl` 只保存经过 identifier 约束的最小完整性字段，不保存
input/output summary。vendor SDK response envelope、HTTP header、stream chunk body 与 hidden reasoning
不进入 ledger 或 debug store。
Qwen 的隐式搜索与网页抓取表现为 generation input 中的
`provider_parameters.enable_search/search_options`、Provider
最终语义回复和 DashScope 返回的来源；Runtime 不为其制造 `tool.started/tool.finished`，也不增加
`tool_count`。
普通前台调用不设置 `response_format`，系统提示词也不要求终态 JSON；因此一次非工具终态只对应
一次 `llm.chat`。主 assistant loop 只接受严格的 `AssistantTextOutput | AssistantToolCall`：
普通回答和自然语言追问都作为非空 `text` 交付，native tool call 归一化为 `tool_call` 后进入工具
治理；Provider refusal、截断、空响应和错误仍是 `ChatResult`/runtime 诊断状态，不扩展 assistant
输出类型。未知类型、空文本和跨变体字段直接校验失败，不静默改写为成功文本。session task-state
更新由 `UserRequest.runtime_task_update` 的 Pydantic 契约承担，不从 Provider 文本推断。

Provider-native Tool 参数未通过 `ActionValidator` 时，assistant loop 将结构化 rejection 与原始
tool-call ID 配对后回灌下一次 Provider turn。可修正的 schema 错误允许一次有界模型修参；首次拒绝
不得提前 `set_response()` 或把 validator message 直出给用户。重复失败或不可恢复的 policy 拒绝进入
answer-only FINALIZE，工具目录被清空；最终生成上下文会保留失败 code，但移除仅用于修参的字段路径与
Pydantic 详情。Validator、Executor 与 FINALIZE 的职责因此保持分离：安全校验不放松，执行节点不承担
用户文案，终态也不会宣称被拒绝的 Tool 已执行。

购物展示协议属于终态交付投影，不属于 assistant loop 文本生成。支持
`supports_shopping_detail_v1` 的 Gateway adapter 在 run 完成后从完整 shopping ToolResult 追加
`<detail>`；`AgentResponse.message` 和 conversation history 保留自然语言。若自然语言 token delta
已经提交，Gateway 额外发出一个 `token_streaming=false`、`content_type=detail` 的补充
`response.chunk`，并以包含自然语言和 detail 的 `response.final` 收口。

The compatibility contracts remain supported:

- `ChatAdapter.chat(request) -> ChatResult` remains valid.
- `ChatRequest.stream_callback(text, payload)` remains valid.
- OpenAI-compatible synchronous parsing still accumulates the same final
  `ChatResult` while adapting internal token events to the callback.
- Runtime callback sites normalize deltas through `stream_delta_to_agent_event`
  so provider metadata and runtime-owned `source` values stay consistent.

## Runtime Stream And Result

Events report what happened during a run; results report its terminal outcome.
They are intentionally separate:

| stream level | yielded events | terminal result |
| --- | --- | --- |
| provider turn | `LLMEvent` | `ChatResult` |
| graph runtime | `AgentEvent` | `AgentState` |
| shared assistant service | `AgentEvent` | `AssistantRunArtifacts` |
| realtime backend | `RealtimeAgentEvent` | `RealtimeAgentResult` |

Callers must not reconstruct terminal state from events. Terminal results own
status, errors, output refs, trace ids, conversation-history effects, realtime
task-state effects, and other metadata that is not guaranteed to appear in the
stream.

`AgentGraphRuntime.astream_state()` is the internal resumable graph stream and
returns `AgentRunStream[AgentState]`; it is available only on a Runtime explicitly
constructed with `allow_interrupt=True`:

```python
stream = runtime.astream_state(request, cancel_token=cancel_token)
async for event in stream:
    consume(event)
state = await stream.result()
```

M5 后 Runtime 的稳定公开方法只有 `initialize_session_memory`、`run_state`、`arun_state`、
`astream_state`、`aresume_state`、`areplay_state`、`afork_state`、
`drain_memory_ingestions`、`run_task_quantum` 与 `close`。无人调用的 `run()` 已删除；history 和 thread
retention 不再作为 Runtime facade 方法。唯一临时例外是 `run_work_item`：legacy Workflow DB 的只读
operator gate 仍有 `running=1`、`waiting_input=1`，因此它继续只服务 drain 中的
`workflows/execution.py`，不得被新入口采用，待 Task 9 retirement gate 全部满足后再删除。

`run_assistant_request_stream()` returns
`AgentRunStream[AssistantRunArtifacts]` and preserves the shared service as the
owner of provider/config resolution, runtime construction, conversation
history, realtime task state, context preparation, trace, and final artifacts.
`AgentRunStream.wait()` is an alias for `result()`.

The optional compatibility `EventSink` is forwarded deterministically while
the same events are yielded. At service level, streamed events are also present
in `AssistantRunArtifacts.events`. A worker exception is re-raised after
already-enqueued events drain and is also re-raised by `result()`.

## Durable Task Event Subscription

长时任务进度流与单次前台运行流是两个独立契约。`TaskEventSubscription`
通过 `DurableTaskService.subscribe_events()` 对持久化 `TaskEvent` 提供
cursor-based replay/tail：

- `after` 表示最后已确认消费的 event cursor，重连后只读取更大的 cursor；
- 订阅采用 pull-based async iterator，没有独立 producer queue，消费者读取速度自然形成
  backpressure；
- 每次读取仍经过 `DurableTaskService` 的 identity 校验，事件流不能绕过任务归属边界；
- 取消或关闭订阅只结束观察，不取消 durable task；
- 默认在任务进入 waiting 或 terminal quiescent 状态且已有事件排空后结束订阅，调用方后续可从
  cursor 重连；`stop_on_quiescent=False` 只用于需要持续 tail 的内部消费者；
- 订阅只读取持久化事实，不拥有 task transition、lease、checkpoint 或 terminal result。

因此 `AgentRunStream` 仍表示一次前台 runtime run，`TaskEventSubscription` 表示一个可跨进程
重启的 durable task 的观察窗口；两者不能互相替代，也不应共享内存队列作为事实源。
DurableTask 当前只作为 schedule、external event 和 notification/checkpoint 的执行底座；
它没有模型可见的 plan-submit Tool，也不再承担新长流程的顶层 Planner 或 DAG authority。

Durable Workflow 使用相同的分离原则，但事件事实源是 `WorkflowStore`：

- `GET /workflows/{workflow_id}/events?after=<cursor>` 先经 `WorkflowService` 做 `user_id + agent_id`
  校验，再读取事务内与 revision 一起提交的 `WorkflowEvent`；
- 前台 Durable Workflow start run 在返回结构化 handle 后结束；成功响应可以没有文本，本身不 tail 后台事件；本地 `media_simulator.py` 可在
  前台终包后另开 identity-scoped HTTP pull 窗口，按 cursor 观察同一 Workflow，但不延长或重新打开
  ingress Gateway run；
- 显式 Workflow mode start 不调用 Provider 或本地 Tool，只持久化用户意图并形成无文案的结构化终态。
  所有 Workflow submission 都先产生仅含 planner item 的 version 1 bootstrap Plan；首个后台
  planner run 由主 Agent 生成严格 `WorkflowPlanProposal`，Definition materialize 和 admission
  后提交 version 2 DAG。得到批准的依赖图可 fan-out 无依赖工作、在受限范围做局部返工，
  并在依赖满足后进入验证或 synthesis；其后
  bootstrap planner 是内部控制节点，不投影为用户阶段、标题或完成数。Plan admission 后，status facade
  只从持久化当前 item 的 LLM 生成 `display_title`、状态和完成数投影产品 `progress`；原始事件
  仍是诊断事实，但不是默认产品文案；
- planner 和每个语义 worker item 都产生独立 bounded `AgentGraphRuntime` canonical run/trace，Workflow event 通过
  `workflow_id/work_item_id/attempt` 关联；LangSmith native graph 保留真实执行父子关系，不把多个
  canonical run 伪装成一次连续 ReAct；它们共同属于一个 durable Plan-and-Execute execution；
- waiting-input、cancel、retry、local plan revision 和 terminal 都是持久事件；客户端断线只丢失
  临时观察窗口，使用 cursor 可重放；
- 当前 HTTP facade 是 pull/replay，不建立长期 WebSocket producer，也不把消费者速度耦合到 worker。

当前持久恢复边界刻意放在 work-item run 之间：`DurableWorkflowWorker` 可同时原子 claim 多个依赖已满足的
work item，每个 item 使用独立 lease、attempt 与预算预留；这些 bounded Agent run 在锁外并行执行，
再分别以 Workflow revision CAS 提交结果。最后一个依赖成功提交后才解锁 join/synthesis 节点。
进程重启后从 `WorkflowStore` 恢复，过期 lease 只回收对应 item；执行期间 heartbeat 续租，防止长模型
run 被误判为崩溃。当前实现没有声称可以从一次 Provider/Tool 调用的中间指令继续，也没有把 LangGraph
SQLite checkpointer 作为第二事实源；崩溃在提交前发生时，该 work-item run 会按 lease/retry 语义重做。
因此普通首批内置 definition 只给 work item 暴露只读 Tool；
`deep_research` 是当前例外，它暴露零个本地 Tool，并在相同 Chat Completions Provider turn 内使用
百炼原生联网。未来若允许写副作用，必须先增加 operation-level idempotency key 和 side-effect
commit barrier。

普通 work-item 的完整最终文本直接作为成功结果。Provider 截断、错误、拒绝、timeout 或空终态属于
技术失败，必须进入 work-item retry/failure 状态，不能把用户可见兜底文案写成成功 artifact。只有
trusted work-item prompt 返回完整、通过严格 schema
校验的 `workflow_control` JSON 时，adapter 才会把它解释为 `verified`、`repair`、`blocked` 或
`failed`；Markdown 代码块、混合文本和未知字段都不会成为控制指令。普通 owner work item 仍可直接
返回正文；结构化 constraint 指定的 verifier 即使成功也必须返回 `verified` 并完整覆盖分配给它的
constraint ID，否则该 work-item run 进入 retry/failure。`repair_work_item_ids` 只能从 controller 提供的
祖先候选中选择，并在 plan revision 前再次经过 DAG/descendant 校验。
重试不是盲重放：下一次 trusted assignment 会携带已提交的前次 attempt number、稳定 error code 和
有界 result summary，planner/worker prompt 据此纠正 schema、admission 或执行错误；这些反馈来自
Workflow Store，而不是入口临时状态。
Planner 的 proposal 解析与同一套 Definition materialize/admission 在该 Agent run 发布 terminal
canonical event 之前执行；解析或 admission 失败会使这次 planner Agent run 本身失败，再由 Workflow
controller 按已提交的 retry/failure 规则收敛，避免内外两层对同一次尝试给出相反终态。

## Thread Model And Ordering

The shared assistant service uses the native asynchronous graph path for its
production stream:

```text
async consumer
  -> run_assistant_request_stream()
  -> run_assistant_request_async()
  -> AgentGraphRuntime.arun_state()
  -> compiled graph astream(v2, custom/tasks/checkpoints/...)
  -> custom RuntimeProductFact
  -> ProductEventProjector -> EventSink.emit(AgentEvent)
  -> AsyncQueueEventSink
  -> AgentRunStream in owning event loop
```

`AgentRunStream` publishes event and terminal records directly when the caller
already runs on its owner event loop. `asyncio.Queue` is not thread-safe, so a
sync LangGraph node or explicit compatibility caller running in another thread
is detected and routed through `loop.call_soon_threadsafe()` instead. Both paths
preserve prior event publication before the terminal sentinel.

The realtime backend normally consumes the shared service stream with
`async for`. The default HTTP Gateway composition root injects
`GatewayRuntimePool.run_request_stream()` and keeps its runtime lease until the
inner stream reaches a result or exception; the Agent-Service composition root
injects `AssistantRuntimeApp.run_request_stream()`. `GatewayRuntimeAdapter` only
accepts an async `run_request_stream=` boundary; the former synchronous
`run_request=` worker-thread bridge and `AgentGraphRuntime.run_stream()` bridge
have been removed. The synchronous `run_assistant_request()` and
`AgentGraphRuntime.run_state()` APIs remain available for existing non-Gateway
callers; there is no response-only `AgentGraphRuntime.run()` alias, and these APIs do not
define a product stream path.

Async migration remains selective:

- keep Gateway, WebSocket, realtime delivery, and supported provider streams
  async-native;
- keep sync-only SDKs, governed tools, local memory, filesystem/artifact work,
  and subprocess-backed operations behind sync/thread boundaries unless
  measured concurrency or latency justifies a focused migration;
- do not duplicate business logic merely to remove `asyncio.to_thread()`.

Completed-turn long-term-memory ingestion is a separate post-response thread
boundary. `AgentGraphRuntime` emits `final_response` and immediately records
`response.delivered` from the Runtime final answer, then freezes a sanitized
`CompletedTurn` and submits it to the bounded `MemoryIngestionQueue`
without waiting for memory Provider I/O. Its trace span carries
`execution_phase=post_response_background`; Agent-Service critical-path and
active-stage accounting exclude that background span. Runtime close drains
accepted work within the configured shutdown bound. Detailed identity,
ordering, saturation and eventual-consistency rules remain authoritative in
`docs/memory-service-architecture.md`.

Proactive messages are a separate non-turn delivery contract. An LLM-authored
Tool input may precompose message content, but a later Runtime event only creates
a typed `ProactiveMessage`; it does not start another assistant loop or emit a
second `final_response`. Runtime-owned background delivery tasks apply bounded
timeout and reminder state transitions, while the entry-owned
`ProactiveMessageSink` arbitrates with active channel turns and reports an
explicit delivery scope. Connection-ephemeral sent events are projected into
the next turn as bounded session evidence and are cleared on disconnect; they
are neither conversation history nor long-term memory.

`ProviderStreamingTurnRunner` bridges an async provider stream into the current
synchronous runtime turn. It uses an event loop directly when called from a
normal worker thread and isolates the coroutine in a helper thread if the
caller already owns a running loop. This is a compatibility bridge, not a
second agent runtime.

## Realtime And Gateway Mapping

`GatewayRuntimeAdapter` consumes the shared assistant stream, maps each
`AgentEvent` through `assistant_agent.gateway.runtime_event_mapping`, and awaits the
realtime event sink. Mapping may produce progress, response chunks, final
response, tool/trace display events, or errors. Final
response chunking and duplicate streamed-delta suppression remain realtime
adapter policy.

Gateway then maps `RealtimeAgentEvent` records to normalized frames, including
`response.chunk -> stream.chunk` and `run.progress -> event.progress`, while
owning run/session lifecycle, reconnect, cancel, interrupt, stale-output
suppression, and transport behavior. Changes to those wire semantics belong in
`docs/gateway-architecture.md`, not here.

Qwen realtime vision 的 Provider delta 与用户可见 Agent stream 是两条独立流。后台
`QwenRealtimeVisionAdapter` 为每次 observation 建立新的 WebSocket/Provider conversation，在该连接内
累积 `response.text.delta`，直到收到 completed `response.done` 后才发布一个结构化
`VideoUnderstandingResult`，随后无论成功、失败或响应不完整都关闭该连接；这些 delta
不会映射为 `LLMEvent`、`AgentEvent(response_delta)`、`RealtimeAgentEvent(response.chunk)`
或 Gateway `stream.chunk`。最终 Agent stream 仍只来自前台 chat Provider，其公开
`token_delta` 经 `AgentRunStream` 实时进入 realtime/Gateway；视觉 Provider 的首 delta 与总耗时
只作为 prompt-safe scalar diagnostics，不携带 Qwen 原文或 raw event。
全语义选帧的 `semantic_frame.*` 是独立的内容安全 side stream，不映射为用户可见 delta 或 Gateway
frame；其事件只描述固定准入、latest-wins 替换和选帧结果。

## Cancellation And Failure Semantics

Cancellation is cooperative:

```text
Gateway/event-like cancel token
  -> run_assistant_request_stream(..., cancel_token=...)
  -> AgentGraphRuntime.run_state(..., cancel_token=...)
  -> raise_if_cancelled()
  -> runtime nodes and ToolExecutor checks
```

The runtime recognizes tokens with `is_cancelled()`, event-like `is_set()`, or
a boolean `cancelled` attribute. Governed retry backoff checks cancellation in
short intervals. A cancellation handled by the runtime yields its existing
`task_cancelled` event and cancelled terminal state; the stream facade does not
invent a second cancellation protocol.

There is no safe force-kill guarantee for arbitrary blocking work:

- `asyncio.to_thread()` cannot terminate a worker thread;
- a blocking SDK, tool, subprocess, or filesystem call may continue until it
  returns, reaches its timeout, or performs a cooperative check;
- closing/cancelling a provider stream is adapter-specific and must preserve
  provider error and resource-cleanup behavior.

Timeouts, bounded calls, adapter cleanup, structured errors, and cooperative
checks are the supported controls. Do not claim hard preemption without a
separate process or an upstream API that actually provides it.

## Session embedding side stream

稳定的 `request.text` 在建立 `AgentState` 后可进入 session embedding coordinator；音频已在上游转成
文本，Runtime 不处理语音 embedding。只有 coordinator 声明 text consumer 时才编码。Image/text
embedding、consumer dispatch 和 temporal retention 是独立的内部 side stream，不替代 LLM/provider
stream，不把向量或媒体证据写入 `AgentEvent`、conversation history 或主 prompt。Runtime pool 实例共享
同一个 coordinator store；session/user 删除和 pool close 清理它，WebSocket reconnect 不清理。
完整契约见 `docs/multimodal-embedding-architecture.md`。

## Source Ownership

| source | responsibility |
| --- | --- |
| `src/assistant_agent/providers/llm_events.py` | `LLMEvent`, provider error/tool delta schemas, accumulator |
| `src/assistant_agent/runtime/chat_adapter.py` | sync chat compatibility, async provider adapters, vendor chunk normalization and cleanup |
| `src/assistant_agent/runtime/provider_streaming.py` | runtime-local async provider stream consumption into `ChatResult` |
| `src/assistant_agent/runtime/llm_event_mapping.py` | visible token delta to `AgentEvent(response_delta)` mapping |
| `src/assistant_agent/runtime/events.py` | runtime `AgentEvent` contract |
| `src/assistant_agent/runtime/event_stream.py` | `AgentRunStream` and thread-safe queue sink |
| `src/assistant_agent/runtime/runtime.py` | graph lifecycle, provider-path selection, `run_state`/`run`/`run_stream` |
| `src/assistant_agent/runtime/runtime_host.py` | composed Runtime and trace-store ownership/close boundary for real entries |
| `src/assistant_agent/memory/ingestion_queue.py` | bounded post-response turn-ingestion queue, per-identity ordering, drain and shutdown |
| `src/assistant_agent/runtime/assistant_run_service.py` | shared sync/native-async run service, stream projection and `AssistantRunArtifacts` |
| `src/assistant_agent/gateway/runtime_adapter.py` | assistant stream consumption and realtime terminal result |
| `src/assistant_agent/gateway/runtime_event_mapping.py` | `AgentEvent` to `RealtimeAgentEvent` mapping |
| `src/assistant_agent/gateway/event_mapping.py` | realtime event to Gateway frame mapping |

Adjacent authorities remain authoritative for their domains:

- `docs/gateway-architecture.md`: Gateway frames and lifecycle.
- `docs/tool-calling-architecture.md`: tool validation/execution governance.
- `docs/observability-harness.md`: trace events, persistence, and redaction.
- `docs/context_engineering_status.md`: prompt/context assembly and budgets.

## Update Rules

Update this document in the same change when any of the following changes:

- an `LLMEvent`, `AgentEvent`, or `AgentRunStream` contract;
- provider stream selection, accumulation, callback compatibility, or cleanup;
- stream/result ownership or terminal exception behavior;
- worker-thread/event-loop ordering or queue bridging;
- cancellation guarantees or blocking-call limitations;
- source ownership or realtime mapping before the Gateway frame boundary.

Update the Gateway authority instead when frame names, session/run lifecycle,
cancel/interrupt delivery, reconnect, or WebSocket behavior changes. Historical
files under `docs/superpowers/specs/` and `docs/superpowers/plans/` are
development records, not current architecture authority.

## Offline Validation

Run the stable core safety net (bare pytest collects only `tests/core`):

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

It protects runtime completion, provider timeout termination, cancellation, and the core event-to-Gateway
conversion contract. Broader realtime behavior is validated through the explicit offline simulators in
`scripts/README.md`; real provider streaming requires `MULTIMODAL_AGENT_PROVIDER_MODE=real` and local
untracked credentials.

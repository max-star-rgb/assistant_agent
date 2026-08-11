# Observability Harness

Last updated: 2026-08-11

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Runtime observability 与日常审计的当前权威 |
| Owns | canonical trace、OTel/Langfuse 投影、redaction、runtime audit、Live Observation Rule |
| Does not own | Release Review Dataset、Scenario、Experiment 与 task-level Score |
| 源码与 schema 入口 | `src/assistant_agent/observability/`、`scripts/run_runtime_audit.py` |
| 验证入口 | `docs/authority.toml` 中 `runtime-observability.verification` |
| 相邻 authority | 上线前 Release Review 见 [`../evals/README.md`](../evals/README.md)；真实运行诊断见 [`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md) |

本文档是 `assistant_agent` 当前 observability 架构、trace 语义、日志边界与
redaction 规则的权威入口。它定义系统必须保留的稳定机器事实和各观测面的职责，不复制
Pydantic 字段全集、脚本参数全集或外部 UI 的展示细节。

真实测试、真实通话、真实 run/trace 或机器日志的具体定位步骤见
[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md)。上线前 Release Review 的
Dataset、Experiment 和发布证据边界见 [`../evals/README.md`](../evals/README.md)。两条链路可以复用
`assistant_agent.quality.*` 的稳定正向 Score 语义，但不共享触发、案例、采样或发布状态：runtime audit
持续检查线上日常 trace，Release Review 仅由 operator 在上线前显式运行。

## 定位与权威边界

Observability 是运行时行为的只读投影，不是另一套执行状态机：

- Assistant Runtime、Gateway、Tool、Memory 和 Provider 各自拥有业务状态；观测层记录它们已经发生的事实。
- canonical trace 是 Assistant run 内部决策、阶段、工具、错误和终态的详细诊断来源。
- `assistant.turn.summary` 是一个 turn 的身份与终态摘要；摘要不能覆盖更细粒度的 raw timeline 事实。
- Gateway lifecycle 和 Agent-Service delivery audit 分别证明入口生命周期与媒体发送/ACK 状态；它们不能由
  Assistant terminal status 推导。
- Langfuse、OpenTelemetry、结构化查询和评估分数都是 canonical trace 的投影或派生视图，不得成为冲突的第二事实源。

发生冲突时按以下顺序判断：当前源码和测试高于 prose；同一运行中，原始 machine event 高于派生摘要，
派生摘要高于派生查询文案；Git 历史只用于解释演进，不证明当前行为。

观测失败原则上必须 fail-open：日志、trace persistence、export 或 score 写入失败不得改变 Agent 业务结果。
但“观测缺失”必须保留为诊断限制，不能据此声称某一步没有发生。

## 观测面及职责

| 观测面 | 当前职责 | 不是其职责 |
| --- | --- | --- |
| Canonical trace | 记录一个 Assistant run 的事件时间线、span 关系、结构化摘要和错误 | 证明 WebSocket 客户端已经处理最终响应 |
| `assistant.turn.summary` | 提供稳定、prompt-safe 的 turn 身份和终态摘要 | 替代 raw trace 或重建所有中间步骤 |
| Gateway lifecycle JSONL | 记录 session、queue、admission、run、cancel、interrupt 和 terminal 入口边界 | 承载 Assistant loop 内部推理或 Provider payload |
| Agent-Service delivery audit | 记录媒体响应从 accepted 到 sent/acked/failed/disconnected 的交付状态 | 表示 Assistant 任务本身成功或失败 |
| Operational console/text log | 提供低噪声、prompt-safe 的运行提示和兼容文本投影 | 作为 runtime 详细开发 timeline |
| Local trace-content overlay | 在明确边界内保存当前 turn 的 request/response、Provider 语义证据、tool observation，以及显式启用的 Mem0 change text | 写入 conversation history 或公开 trace summary |
| OTel/Langfuse projection | 把 canonical events 映射为 trace/span/generation、usage 和安全 metadata | 改写 canonical event 的含义 |
| Metrics、scores、eval views | 从 canonical facts 派生统计、诊断和质量分数 | 反向控制 runtime 行为 |

默认本地文件包括：

- `.data/graph_trace.jsonl`：canonical Assistant trace 的后台持久化副本；
- `.data/gateway_events.jsonl`：prompt-safe Gateway lifecycle；
- `.data/agent_service_delivery.jsonl`：Agent-Service delivery audit；
- `.data/logs/gateway.log`：Gateway lifecycle 的兼容文本投影。

这些文件都是单机诊断数据，不是跨进程、跨主机或 durable delivery 的权威数据库。

## 关联标识

标识必须按职责使用，不能仅因值相似而互换：

| 标识 | 语义 |
| --- | --- |
| `trace_id` | 一个 Assistant run 的 canonical 查询键；普通前台 run 同时用作 Langfuse `assistant.turn` 关联键，Workflow work-item 的导出身份见下文 |
| `run_id` | 一次执行，从 Gateway ingress 进入 Assistant Runtime 并到达运行终态或取消边界 |
| `turn_id` | Gateway/session 入口分配的 turn 身份，用于入口生命周期关联 |
| `delivery_id` | 一个 Agent-Service 媒体响应的发送与可选 ACK 生命周期 |
| `session_id` | 会话范围；canonical trace 可保留内部值，Gateway prompt-safe 投影使用稳定摘要 |
| `user_id` | 用户范围；是否保留原值取决于观测面，Gateway 投影只保留稳定摘要 |

Gateway 创建的 `run_id` 必须原样传入 Assistant Runtime。Runtime 在建立 trace 后发布 `trace_id`，
Gateway lifecycle、turn latency 和 delivery audit 在获得该标识后补齐关联。`delivery_id` 只证明媒体交付，
`acked` 也不能替代 `run.completed`；反过来，Assistant completed 也不能证明消息已经送达媒体应用。

对外部系统提供的 legacy trace seed，OTel 映射可生成稳定的 W3C trace ID；查询和诊断时必须使用实际
machine record 中的最终 ID，不根据格式猜测关联关系。

## Canonical trace 与 span

`src/assistant_agent/observability/trace_store.py` 中的 `TraceEvent` 是 canonical event 容器。
它统一承载身份、event name、observation type、span 关系、时间、状态、有限 attributes、结构化
input/output summary 和安全错误。具体字段以该 model 为准。

### Timeline 语义

- `canonical_event` 表示跨 observer 和 viewer 使用的稳定事件名；`event_type` 保留底层事件类别。
- `span_id` / `parent_span_id` 表示因果和层级，不用列表位置推断父子关系。
- 成对的 started/terminal event 必须共享同一 span 身份；terminal 可以是 finished 或 failed。
- started event 的时间是 span start，terminal event 的时间是 span end；不能用 terminal 时间倒推完整 wall time。
- `latency_ms` 表示该观测边界测得的 wall time。Provider 或 Tool 自报 latency 是嵌套诊断，不能替代外层 wall time。
- observer 可以丢失或持久化失败，因此缺少 terminal event 可能表示仍在运行、被中断或证据不完整；不得伪造 finished span。

稳定生命周期包括 run、LLM、Tool、Memory、Context、response 和 runtime postprocess 等边界。
事件全集由各 owning module 构造，本文档不维护重复清单。

VLM Provider 调用使用成对的 `vlm.infer.started/finished`，terminal 投影为名为 `vlm.infer` 的
generation。同步视觉 Tool 的 generation 通过 `parent_span_id` 挂在对应 `tool.execute` 下；纯视觉
Store/embedding/reminder 操作不伪造 VLM generation。事件只允许 capability、source、media kind/count、
prompt version、Provider/model、latency、usage、状态和稳定错误码，不记录视觉正文、JPEG/base64、媒体
ID/路径或 Provider 原始响应；错误消息固定为通用安全描述。
视觉理解 Tool 自身同时使用 `metadata_only` canonical lifecycle policy，防止
`tool.started/finished` 或远程 exporter 旁路上述边界。启用本地 trace content 且 exporter
指向 loopback Langfuse 时，进程内 content overlay（以及显式开启的本地 `trace.content` 诊断记录）可以
额外保存经过独立 allowlist 的语义内容：
Tool execution span 展示不含媒体身份、evidence、向量和 Provider raw payload 的安全 ToolResult；
`tool.observation` 展示进入 Context budget 前的完整语义 observation。该 policy 不删减主 LLM 实际消费的
Tool observation。VLM event 与后台 summary 的观测写入均 fail-open；写入失败不能阻止 Provider 调用或
把成功视觉结果改成业务失败。

VLM 的安全输入和归一化结构化结果可以进入与 canonical trace 分离的进程内 content overlay。输入只允许
mode、prompt version、adapter 实际 resolved instructions、query、media kind、frame sequence/count、
history frame count 和 memory context 是否存在；不允许媒体引用、JPEG/base64、媒体字节、embedding 或
Provider raw request。输出只允许 summary、scene、objects、people、actions、events、OCR、grounding
列表、confidence 和 Provider/model/latency 等已校验字段。VLM input 只有在本地 trace content 开启且
exporter 指向 loopback Langfuse 时才投影到 `vlm.infer` generation 与 `vision.runtime` root input；输出按
既有本地 content export policy 投影。canonical event 与 JSONL 始终不保存 prompt、query 或视觉正文。

统一 embedding side stream 额外发布 `embedding.requested/deduplicated/started/finished/failed/
dispatched/consumer_dropped/session_cleanup`。这些事件只允许 modality、dimension、latency、priority、
consumer count、错误码和稳定 digest；不得包含向量、文本、图片/evidence 路径或原始 session、
observation、revision、space 标识。具体投影见 `media/embedding/observability.py`。

实时全语义流水线额外发布 `semantic_frame.admitted/skipped/replaced/selected`。只允许哈希化 session、
frame sequence、替换 sequence 和稳定 reason；不得包含 JPEG/evidence 路径、图像内容、VLM 文本、向量
或原始身份。`replaced` 用于证明 latest-wins 生效，不表示发生错误或积压。
默认 Runtime 为 session embedding coordinator 注入结构化 logging observer，因此后台视频没有活动
Agent turn 时仍会产生这些 content-safe side-stream 事件；测试也可注入内存 observer。未知 reason 统一
投影为 `other`，不能借 reason 字段旁路脱敏。
同一 observer 还发布 `visual_semantic.retained/evicted/index_failed`、`visual_memory.query` 与
`visual_memory.compaction`；只记录
哈希化 session、sequence、稳定 status、数量和 latency，不记录 VLM 文本、用户 query、向量或 evidence。

视觉上下文预算事件为 `visual_context.preflight/compacted/compaction_failed/hard_limit`：独立调用
`VisualContextService` 时，它在初始预算评估、成功 CAS 后重建、compactor 失败和最终 hard 拒绝处分别发出。
当前 Agent-Service realtime observer 使用单帧、无历史的 VLM 输入，不构造该 Service，因而不会产生
这些预算事件；observer 的工具 metadata 仅标记 `visual_context_compaction.status=disabled_single_frame_text`。
payload
只能包含哈希化 session、sequence、input token 数、effective input limit、target token 数、usage ratio、
covered/recent count、summary revision、latency、compacted 布尔值和 allowlist 内的枚举 status；未知 status 归一为
`other`。`revision_conflict` 表示 CAS loser 已按同一 video/as-of 重读 winning summary 并重建 pack；
随后是否继续或 hard fail 必须以这个新 pack 为准。covered count 来自代码拥有的有界 coverage metadata，
事件和模型投影都不携带 record ID 或 coverage digest。这些事件绝不记录
视觉全文、summary、用户 query、record/video/observation ID、evidence/path、向量、JPEG 或 Provider
raw response。hard limit 事件只表示最终 Qwen/VLM observation 未调用；此前可能为预算收敛调用独立
LLM visual compactor，按当前状态机最多两次。事件写入仍是 best-effort/fail-open，缺少事件不能改变
或反推视觉 Provider 调用结果。

### ReAct、phase 与重试

`react.iteration` 表示一次模型决策循环；Runtime phase 由 `runtime.phase.changed` 及 LLM event 上的
`run_phase` 记录。`loop_guard.triggered` 的具体处置必须读取结构化 disposition，不能从事件名称推断
一定进入 FINALIZE 或 terminate。

同一次 Provider request attempt 使用一对 `llm.chat.started` / `llm.chat.finished`；attempt kind、
phase、usage、wall latency 和 Provider latency 是该 generation 的结构化诊断。Tool executor 内部的
Provider 重试属于同一 Tool span；模型修改参数后发起的新 Tool call 才是新的 action。

FINALIZE、protocol retry 和 guard 的运行规则属于
[`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md) 与 runtime 源码；
observability 只要求真实 phase、attempt、阻止执行和终态被记录，不在此复制控制流实现。

### Trace query

`TraceStore` 支持按 run、trace 和 user 查询以及按 user 删除。`TraceQueryService` 从 redacted events
生成 `RunSummary`、`TraceSummary`、tool call summary 和 context report。查询 summary 是方便读取的
派生结果；需要判断事件因果、部分失败或未闭合 span 时必须回到 raw events。

## Turn、delivery 与 latency 摘要

### Assistant turn summary

每个普通 terminal turn 应追加一个 prompt-safe `assistant.turn.summary`，当前 schema 为
`assistant_turn_summary_v2`。它集中表达：

- trace/run/turn/session 的关联身份；
- client、entry、runtime 和 terminal status；
- 是否存在响应以及 Tool/Error 的有界计数；
- 可选的安全失败摘要和 Agent-Service latency 引用。

普通 runtime turn 在 postprocess 后写 summary。Agent-Service turn 由入口延后到 delivery latency 已建立后
写入，从而保持同一 schema 且每个 terminal turn 只有一个摘要。字段、枚举和兼容读取规则以
`src/assistant_agent/observability/turn_summary.py` 为准。

`assistant.turn.summary` 不包含 prompt、用户/助手正文、Provider 原始 payload、Tool request body、
Memory 内容或媒体字节。失败摘要必须经过清洗和长度限制。

### Agent-Service delivery audit

Agent-Service delivery 使用独立的 `agent_service_delivery_v1` JSONL audit。关键语义是：

- `sent` 只证明 WebSocket `send_text()` 返回；
- 只有协商 ACK 且收到有效 `chatResponseAck` 后才是 `acked`；
- failure terminal 不产生可 ACK 的成功 delivery；
- disconnect 必须区分发生在 send 前还是 ACK 前；
- session/chat 身份在 audit 中使用摘要，不保留响应文本、媒体或 Provider payload。

允许的状态转换和校验以 `src/assistant_agent/observability/agent_service_delivery.py` 为准。

### Agent-Service turn latency

`agent_service.turn.finished` 携带当前 `agent_service_turn_latency_v2` 摘要。它把入口、Runtime、Gateway
wrapper 和 WebSocket send 组合为尽可能不重叠的 critical path：

| 层级 | 代表性阶段 |
| --- | --- |
| Media transport | parse、same-session queue wait |
| Assistant leaves | conversation、memory、context、LLM、validation、Tool、finalize、postprocess |
| Gateway/response | Gateway overhead、WebSocket send |
| Residual | 无法由已闭合阶段解释的正 `unattributed` |

Provider/Tool 自报 latency、ACK latency 和视频 freshness 是嵌套或次级诊断，不再次计入 critical path。
最大 critical-path stage 是 bottleneck；`unattributed` 为正时也是合法候选，而不是自动归因给 Provider。
实时视频的 `semantic_publish_latency_ms` 使用进程单调时钟计算从 Agent-Service WebSocket 收到视频消息
到 `SessionVisualSemanticStore.record_success` 完成、记录已可查询的端到端耗时；它包含解码、选帧、task 调度、
视觉 observation、文本 embedding 和 semantic store 写入，不与 chat critical path 重复相加。后台视觉
同时输出 `h264_decode_latency_ms`、`keyframe_selection_latency_ms`、`queue_wait_latency_ms`、
`observation_latency_ms`、`text_embedding_latency_ms`、`semantic_store_write_latency_ms` 以定位本地阶段。
`keyframe_selection_latency_ms` 从视频 ingress 到关键帧选中后扣除 H.264 decode；
`queue_wait_latency_ms` 在并行 VLM 架构中只表示 task 创建到实际开始执行的调度等待，不包含前一关键帧
Provider 耗时；
成功文本发布后进行的独立 WebSocket 关闭握手不属于可查询关键路径，因此不计入
`observation_latency_ms` 或 `semantic_publish_latency_ms`；
Qwen WebSocket 还输出 `jpeg_prepare_latency_ms`、`connection_setup_latency_ms`、
`instruction_update_latency_ms`、`media_commit_latency_ms`、`response_first_delta_latency_ms`、
`response_tail_latency_ms`、`response_latency_ms` 和 `result_parse_latency_ms`。这些字段经
`context.build.finished` 投影到 `agent_service.turn.finished.video`，均为嵌套诊断，不重复计入总耗时。
兼容字段 `first_delta_latency_ms` 仍表示整次 observation 起点到首个 delta；
`response_first_delta_latency_ms` 只表示 `response.create` 到首个 delta。

超时或取消中的摘要可以同时表达 entry failure、runtime pending cancel 和 terminal unknown。
这类摘要是截止某一时刻的事实，不得替代之后出现的 `run.cancelled` 或 `run.failed`。
完整 model 和阶段归并逻辑以 `src/assistant_agent/observability/agent_service_latency.py` 为准。

## Operational logging 与持久化

### Canonical trace persistence

本地 server 使用 `CompositeTraceStore`：进程内 `InMemoryTraceStore` 是即时 primary，后台
`BufferedJsonlTraceStore` 把事件写入 `.data/graph_trace.jsonl`。后台队列有界：队列满、关闭中写入或
secondary 异常会记录 drop/error 状态，但不阻塞业务 response。shutdown 只做有时限的 flush。

因此：

- 进程内查询可能看到尚未落盘的事件；
- 进程崩溃、队列丢弃或 flush 超时可能产生部分 JSONL；
- JSONL 缺事件不能单独证明运行时没有发出该事件；
- persistence、OTel export 和 Langfuse score observer 都是 secondary，失败必须 fail-open。

### Gateway operational logging

Gateway lifecycle 使用 `gateway_lifecycle_event_v1`。JSONL 保留完整可用的 `run_id`、`turn_id`、
`trace_id`，对 `user_id`、`session_id` 做稳定短摘要，只允许 allowlist 内的状态、计数、reason/source
等标量 attributes。未知 client reason 被归类而不原样写入。

Combined console 默认只显示关键 Gateway lifecycle 和普通应用 WARNING/ERROR；runtime 详细 timeline
不投影到 console 或单独的 `runtime.log`。Gateway 兼容 text log 使用轮转文件，文件或 JSONL handler
创建失败时保留 console 并继续启动。精确默认路径、大小和备份数以
`src/assistant_agent/observability/operational_logging.py` 为准。

## Content capture、redaction 与访问边界

本项目本地 trace content 默认启用；设置 `MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT=0` 可进入减少内容的兼容模式。
Provider protocol 语义捕获还受 `MULTIMODAL_AGENT_LOCAL_PROVIDER_PROTOCOL_CAPTURE` 控制。开关语义以
`src/assistant_agent/observability/trace_content_policy.py` 为准。
Mem0 change text 额外要求显式设置 `MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT=1`，并且只允许
投影到 loopback OTLP endpoint；默认 canonical event 仅记录数量、event 计数和 memory ID。
该开关独立于普通 trace/provider 内容权限，不得隐式启用 request/response、Tool observation 或
Provider protocol capture。overlay 写入失败时 canonical event 仍须保留，并用安全状态承认证据缺口。

内容捕获与 prompt-safe trace 必须分层：

- canonical event 的公开/query 投影先经过 redaction 和错误清洗；
- 当前 turn 的用户文本、助手文本、已交付文本、LLM input/output 与 Tool observation 保存在有界、进程内的
  `InMemoryTraceConversationStore`，不写入 conversation history；
- 只有显式允许的 loopback 查询或配置为包含内容的本地 exporter 可以取得该 overlay；
- failed/cancelled turn 的调试内容不能因此进入未来模型上下文；
- credentials、authorization material、hidden reasoning 和 inline binary media 始终不得进入 trace content。

所有 viewer、API、日志和 exporter 都必须在自己的输出边界再次应用最小字段、redaction 与访问限制。
“本地”不是绕过授权或泄露 secret 的理由。

## Langfuse、OTel 与评估投影

`build_text_otel_span_specs()` 将 redacted canonical events 投影为依赖无关的 OTel span plan：

- standard Assistant turn 的根 span 为 `agent.runtime`，Langfuse trace 名为 `assistant.turn`；
- Deep Research ingress 的根 observation 为 `assistant.submit`，Langfuse trace 从入口开始固定命名为
  `deep_research.workflow`；
- 后台实时 VLM 每次 observation 使用独立 run/trace，以 prompt-safe
  `vision.observation.summary` 闭合；根 span 为 `vision.runtime`，Langfuse trace 名为
  `vision.observation`，不能伪装成没有用户 turn 的 `assistant.turn`；
- ReAct iteration 是根 span 下的逻辑 span；
- 声明了 observation type 的 operation 映射为 span、generation 或 event；
- operation 的 parent、start/end、status 来自 canonical span 关系和 started/terminal event；
- exporter 必须先创建 batch 内父 span 再创建子 span；没有 external parent 的 runtime root 是真正 root，
  不能为保持 trace ID 伪造 `0000000000000001` parent；
- root 的 external parent 只接受 `run.started` 显式提供的上游 parent，不能把内部 Tool/VLM
  `parent_span_id` 反推为 root parent；
- usage 映射到 generation/OTel token attributes，不能因嵌套结构而丢失；
- `context.compile` 是不产生 Provider 费用的 span：目标 tokenizer 的
  `compiled_input_tokens`、`effective_input_limit`、`context_token_usage_ratio`、`tokenizer_id` 和
  `token_accounting_status` 自动投影为 Langfuse observation metadata，但不得写入 Usage breakdown；
  `llm.chat` generation 的 Usage breakdown 只记录 Provider 返回的实际 input/output/total，避免同一轮
  preflight 与实际调用重复计量；
- only-allowlisted metadata 和 output reference 可以进入公开 projection。

Deep Research 从前台提交到 durable terminal 使用同一个 `deep_research.workflow` trace。该做法遵循
[Temporal Python SDK tracing interceptor](https://github.com/temporalio/sdk-python)、
[Temporal LangSmith tracing sample](https://github.com/temporalio/samples-python/tree/main/langsmith_tracing)
与 [OpenAI Agents higher-level trace](https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md#higher-level-traces)
的开源模式：
入口把可信 `trace_id` 和产生 Workflow 的 `workflow_submit` Tool span ID 持久化到 Workflow record，worker
恢复后继续把 Workflow 与 work-item observation 导出到该 trace，不依赖原进程中的 ContextVar 存活。
已提交的 `workflow.plan.*`、work-item 终态/重试/返工事件和 Workflow terminal 事实投影出 Workflow
`agent`、Plan `chain` 和 work-item `chain`；每次 work-item 的 `agent.runtime`、context、真实
`llm.chat` 与 Tool observation 以对应 attempt chain 为 parent。

work-item Assistant run 仍保留自己的 canonical `trace_id/run_id`，用于本地事件查询、恢复和审计；仅
OTel/Langfuse 的导出 trace identity 被显式投影为 ingress trace identity。work-item span 输出将该值标为
`assistant_canonical_trace_id`，并只生成当前 Workflow trace 的详情链接，不生成指向不存在的独立子 trace
链接。`workflow_id/work_item_id/attempt_id` 共同证明投影归属。提交阶段的短回复只结束 Gateway run 和
`assistant.submit` observation，不结束整个 Langfuse trace；Workflow terminal 才写 trace-level 最终输出。
Provider-native 搜索只属于真实
`llm.chat` generation，不伪造 Provider 未暴露的内部搜索 span。Workflow 投影只观察 Store 已成功提交的
事件；总览默认只导出 Plan 标题、类型、依赖、状态、计数和 artifact ref，不导出 Workflow/work-item
objective、deliverable/constraint 正文。observer 获取 cursor 或导出失败必须 fail-open，不能影响
lease、revision 或持久状态；cursor 读取使用 Store 的 O(1) latest-cursor 契约，不扫描或重放全量历史。
缺失完整 ingress trace context 时观测链路 fail-open，Workflow 仍提交并退回兼容的独立 Workflow trace。

`assistant.turn.summary` 到达时主 trace 可以先导出；Runtime 随后发出的 `response.delivered` 与后台
`memory.ingestion.finished` 都不得被静默丢弃。OTel observer 将它们作为单独的 late span 追加到同一 trace，
parent 使用稳定的 `agent.runtime` root span ID。已有 `langfuse.session.id` 负责在 Session 页面聚合
同一会话的多个 turn；Langfuse 不创建第二套 session 或 memory 数据库。

后台 `vision.observation` 只通过同一 `langfuse.session.id` 与会话聚合；它不创建 Assistant turn summary、
conversation history 或主 LLM generation。其内部仍保留 `tool.execute -> vlm.infer` 因果链，从而分别
观察 Tool 治理耗时和副 VLM Provider 耗时。每条成功 `VisualSemanticRecord` 同时保存产生它的
`source_vision_trace_id`、`source_vision_run_id` 和 `source_vlm_span_id`。`live_view_inspect` 选择缓存记录时只把
实际选中 record 的这些身份以及 `source_visual_record_id`、`snapshot_sequence` 作为 prompt-safe metadata
投影到主 turn Tool observation；不得用 Session 时间邻近或全局最新 trace 猜测来源。投影目标为
loopback Langfuse 时，还会根据精确的 `source_vision_trace_id` 生成
`source_vision_trace_url`，供 UI 从 `live_view_inspect` 直接打开对应 `vision.observation`；该 URL 只存在于
Langfuse/OTel 派生视图，不反写 canonical Tool 结果，非 loopback host 也不生成。

前台 `live_view_inspect(query=...)` 还会在自己的 Tool span 下创建独立 `vlm.infer` generation。loopback
input overlay 展示 adapter 实际 resolved instructions、query 和“单帧、无历史、无 memory context”等
结构化上下文，但不记录 JPEG 或 evidence 路径；关闭本地 content、非 loopback exporter 或 overlay
缺失时降级为 query 是否存在、单帧 media metadata 和 prompt version。输出展示脱敏后的 query-specific
VLM 文本。原缓存 record 的 `source_vision_trace_url` 继续指向生产该证据帧的后台 trace，两者分别表达
“本轮如何回答”和“这张证据帧从何而来”。

视觉 Tool 在 Langfuse 中有三个不可互换的内容边界：具体 Tool execution span（例如
`live_view_inspect`）展示安全 ToolResult；`tool.observation` 展示 Context compaction 前的语义 observation；
下一次 `llm.chat.input` 展示 compaction、budget 和 Provider 协议编译后请求的等价可读投影；
OpenAI Chat 形状保持不变，DashScope 的 `input.messages`、`parameters.tools` 和
`parameters.tool_choice` 提升为 Langfuse 可格式化的顶层字段，其余参数归入
`provider_parameters`。本地 content overlay 中按同一 span 保存的原始 Provider request 才是请求形状
审计权威，两者不得改写 message、Tool schema 或参数值。`tool.observation.input` 分别标注
`runtime_tool_call_id` 和
`provider_tool_call_id`，不得继续用同名 `tool_call_id` 混淆内部执行身份与 Provider 协议身份。

后台 `realtime_video_observe` 是视觉结果生产者，不是主 LLM Tool observation。其 `vlm.infer` 必须作为
Tool span 的子 generation；loopback content overlay 可让 `vlm.infer.input` 和 `vision.runtime` root input
展示 resolved instructions、query 及明确的单帧上下文计数。overlay 不可用时只展示 `media_kind`、
`media_count`、`prompt_version`、`source`、可用的 `frame_sequence`、`query_provided` 和
`content_exported=false`。两种模式都不展示图片或媒体引用。来源 URL 只用于跨 trace 消费者；后台 Tool
位于自己的 `vision.observation` 内时不得生成指向自身的 `source_vision_trace_url`。

连接级视觉提醒使用创建 turn 的 correlation 记录 late-capable canonical events：
`visual_reminder.created`、`visual_reminder.matched`、`visual_reminder.delivery.finished` 和
`visual_reminder.cleared`。这些事件只包含 reminder ID、状态、相似度和清理原因等 prompt-safe 摘要；
target、message、向量和媒体内容不得进入 canonical trace。观测写入失败 fail-open，不改变提醒匹配、
主动 message 发布或连接清理。

Langfuse 的 trace、observation、score 和 Dataset/Experiment 是远端投影与评估记录，也是日常人工查看
Runtime trace 的主入口。面板如何折叠或展示长 JSON 不属于架构契约；需要核对机器事实时查询 observation
数据、结构化 trace API，或回到本地 canonical JSONL。

### Langfuse-first Runtime 审计

`scripts/run_runtime_audit.py run` 是只读的日常审计入口。user timer 每天北京时间 00:15
运行，审计刚结束的前一个自然日（北京时间 00:00 至次日 00:00），而不是最近几小时。它以
Langfuse Observations v2 聚合出的完整 trace、observation 和 Score 为主证据；本地 `.data/graph_trace.jsonl` 只生成
trace/run、时间、terminal 与 event count 的完整性 manifest。只有 Langfuse 可读但缺少相应 trace
时，才把有界、redacted 的本地 timeline 作为 fallback evidence；Langfuse 不可读只记录
infrastructure unknown，不能把本地记录误报为导出缺失。本地 JSONL 不存在、不可读或只含无效记录时
必须标记 local completeness unavailable；远端 Trace 非空时仍完成远端审计并在日报限制中公开该缺口。
Langfuse 查询成功且远端 Trace 为空时，成功记录“昨天无运行trace”，本地缺口只作为限制公开，不调用
Codex，也不把正常空窗口写成“审计没有完成”。只有 Langfuse 主证据不可读时才不能认证为空日。

审计以 `state/watermark.json` 记录最近一次自动审计成功的自然日。无参数 `run` 永远只考虑刚结束的
昨天：昨天尚未成功时审计昨天，昨天已经成功时不重复运行；错过调度、机器离线或前一日失败都不会
自动补跑更早日期。`run --date YYYY-MM-DD` 默认同样幂等：该日期已完成时直接跳过；确需重新审计时必须
显式使用 `run --date YYYY-MM-DD --force`，`--force` 不允许省略日期。若某日 Langfuse 查询成功且没有
任何远端 trace 或本地 fallback evidence，会写入一份极简中文成功日报，
并且不调用 Codex。显式日期命令只
以当前 registry 建立只读 lifecycle refresh view 并刷新对应 Markdown，不写 `state/issues.json`，也不
改变连续 watermark。即使是历史日期，伪造的 Trace/Score、未知 issue 直接进入
`runtime_verified`/`regressed`、伪造 commit 或不存在的测试路径仍会被拒绝。完整参数继续以 `--help` 为准。

产物固定写入 `.data/runtime_audit/`：

- `inbox/YYYY-MM-DD.bundle.json`：按被审计日期命名的只读完整 audit bundle；
- `state/codex-inputs/YYYY-MM-DD.codex-input.json`：同日交给 Codex 的有界异常索引；
- `state/staging/`：运行中或失败 attempt 的临时证据，成功发布后清理；
- `state/attempts/`、`state/issues.json`、`state/watermark.json`：内部尝试记录、issue registry 与最近成功检查点；
- `reports/YYYY-MM-DD.md`：唯一面向人的日报；`reports/` 不放内部 JSON。

新收集的内部 inbox 使用 `assistant_agent_runtime_audit_bundle_v2` 紧凑 JSON。collector 可在生成
确定性 finding 时临时读取 Langfuse raw metadata，但持久化前会删除 Trace、Observation 和 Score 的
metadata。Trace/Observation input 中重复的 Tool catalog 按完整 SHA-256 提取到 bundle 顶层
`tool_catalogs`，原位置只保留 `tool_catalog_ref`；Tool 业务 output 中同名字段不参与改写。读取端继续
兼容旧 v1 bundle，v1 只作为历史输入，不再由新收集流程生成。

本地 auxiliary side stream 不作为 Langfuse 导出缺失：视觉工具、session recall 等未形成
`assistant.turn` completeness record 的本地事件只在 bundle 中保存事件类型与数量聚合，不逐条生成
`local_fallbacks` 或 finding。只有真实缺少对应 Langfuse `assistant.turn` 的本地主运行记录才保留有界
timeline fallback。

完整审计 bundle 只供本地机器追溯和发布后校验；Codex 日审计只读取独立的
`assistant_agent_daily_codex_input_v1` 异常索引。本地确定性程序仍扫描当天全部 trace，但索引只包含低分、
缺失 Score、observation error 等异常 trace 的基础事实、Score 和完整 observation 序列。没有异常时直接
生成极简成功日报，不启动 Codex。存在异常时，Codex 只能读取和审计该索引中的 trace ID，输入不暴露完整
bundle 路径，也不得浏览或评价其他正常 trace。runner 将第三层索引和既有 issue registry 作为
`codex exec` 的 stdin 上下文直接交付，不依赖 Codex 子进程再通过 shell 打开证据文件。第三层还包含从
审计窗口开始到本次采集时刻的有界 `repository_changes`：完整 commit SHA、提交时间、subject、改动文件
和优先覆盖 `src/assistant_agent`/`tests` 的 patch 摘要。Codex 只有在提交晚于对应坏 Trace 且代码事实能
证明处理同一根因时，才能把问题标记为“已修改，等待实际验证”；时间接近本身不构成修复证据。索引以
500,000 bytes 为硬上限；超限时优先省略异常 trace
中的大 input/output 内容，同时保留 observation 身份、大小和省略原因。结构本身仍超限则审计失败，不能
静默丢失异常范围。

每个日期的状态机为 `running -> succeeded` 或 `running -> failed`。`attempt_id` 仍保存实际运行时间，
但不进入三份正式产物的文件名。运行时先写 staging；成功时以 journal 校验日报、issue registry 和
watermark 的前置条件，再把第二层、第三层和日报按单文件原子写、整体可恢复的方式发布到同一个日期。中断后的待提交 journal 会在下次
运行先恢复，冲突的旧 journal 会隔离。失败只保留内部 attempt/staging 诊断证据；它不会覆盖同日已有成功
bundle、Codex input 或日报，也不会推进 issue registry 或 watermark。

自动连续日报发布 Markdown 前必须从 previous/merged registry 生成确定性 issue view，使持续
`open`、`regressed`、`code_addressed` 和 `uncertain` 不因 Codex 当日漏发而消失；显式日期 refresh 不改
registry，其人读 Markdown 只使用本次 Codex 已归并的问题，避免旧标题和旧正文与当前归纳重复出现。
`uncertain` 只作为暂时不能下结论的事项，不进入当前修改建议；`runtime_verified` 只在当日真实转换时出现。
空日或无异常日不调用 Codex，但仍以自然语言保留 active/code-addressed 问题标题。面向人的 Markdown 是结论先行的
对话式投影，不生成证据附录，也不显示 issue key、Score/observation/run ID、commit SHA、测试路径或内部
生命周期术语。唯一例外是每类问题下的“最近的相关记录”：renderer 从本次完整 bundle 中匹配已经通过
归属校验的真实 `assistant.turn`，按北京时间从近到远列出最多 3 条，明确显示 `session_id` 和 Trace ID，
并以 Langfuse Python SDK 公共 `get_trace_url()` 返回的详情地址为可信基址，确定性转换为 Trace 列表页的
展开链接。链接保留“排除 `vision.observation`”过滤器，并通过 `peek`、UTC `timestamp` 与
`peekView=expanded` 直接展开目标 Trace，方便继续查看前后记录。Codex 不接触、不生成也不复制这些 ID
或 URL。URL 获取失败、缺失、不是 `http/https`、包含用户凭据或地址中的 Trace ID 不匹配时，日报保留
Session 与 Trace ID，显示“Langfuse 链接暂不可用”，但审计本身继续成功；没有可匹配记录时不显示空列表。

renderer 对自然语言段落设置人读长度预算，定位记录不计入正文预算，但由每类问题最多 3 条的上限控制；
五个极端长问题的自然语言正文仍不得超过 1,500 字，正常日报目标仍为约 500～1,000 字。完整机器证据仍
保存在第三层 JSON 与 issue registry 中；Score 证据复用现有
`trace_evidence_refs`，格式为 `trace:<trace-id>/score:<score-id>`，并按 current bundle 校验归属。

确定性第三层存在异常时，Codex 通过 `--ephemeral --sandbox read-only` 运行；子进程环境移除
Langfuse 和 Provider credentials，强制 mock Provider mode，只允许生成报告，不允许修改代码、Langfuse
或 Memory。Codex 网络必须经过 operator 明确配置：runner 只透传无用户名密码且指向
`127.0.0.1`、`localhost` 或 `::1` 的标准 proxy 环境变量，远端或带凭据 proxy 仍被删除。systemd
不会自动继承交互终端变量；需要本地代理时在未跟踪的
`%h/.config/assistant_agent/runtime-audit.env` 中配置 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 与
对应小写变量。Langfuse、Codex、报告 schema、Judge pending 或 evaluator 基础设施失败都不是质量失败，
而是上述不可推进的审计失败。issue 的代码修复只能标为 `code_addressed`；必须在后续自然日取得新的
运行证据，才能升级为 `runtime_verified`，新 trace 证明复发时才标为 `regressed`。首次发现 issue 时若
本次坏 Trace 之后已经存在可信代码处理，可以直接标记 `code_addressed`；`code:<commit-sha>` 必须解析为
当前本地仓库真实 commit，`test:<repo-relative-path>` 必须是仓库 `tests/` 下存在的文件，进入转换的代码
证据相对 previous 必须为新，regressed 后不得复用旧证据。能取得 commit 时间时还必须晚于引用的坏 Trace；
仅凭时间相近不能虚构 owning-module 关系。

Codex schema 对正文、issue、evidence 和 limitations 设置长度/数量上限；prompt 要求像直接回复维护者一样
使用普通中文，并禁止在正文字段写入机器 ID、内部状态、完整用户对话、Memory 正文或 Provider 原始响应。
机器引用只允许进入结构化 evidence ref 字段，最终 renderer 还会独立清除意外混入正文的技术标识。发布前
还会比较 Codex 正文字段与 bundle 中敏感长文本，若出现长片段
重合，则保留内部 Codex JSON 用于诊断，但只记录失败 attempt，不发布成功日报、不写 registry、不推进
watermark。所有进入 attempt、CLI stderr、日报和 commit journal 的错误先经过同一 credential 与 URL
userinfo 清洗边界。

统一质量 Score 名称为：

| Score | 目标与来源 |
| --- | --- |
| `assistant_agent.quality.response_quality` | 日常 Trace 最终文本回答质量；来自 Live Observation Evaluator |
| `assistant_agent.quality.grounding` | 日常 Trace 最终文本对工具/上下文证据的忠实度；来自 Live Observation Evaluator |
| `assistant_agent.quality.tool_result_quality` | 单个 `tool.execute` observation 的结果语义质量；只使用 observation evaluator |
| `assistant_agent.quality.memory_extraction` | 单个 `memory.turn_ingestion` observation 的长期记忆提取质量 |
| `assistant_agent.quality.memory_recall` | 具有实际召回证据的 memory/LLM observation 的召回质量；证据不足时保持 missing/unsupported，不伪造失败 |

Score name 只表达测量对象；`source`、judge/model、evaluator version、live/experiment mode 放 Score
metadata。terminal、Tool 调用成败、event count、latency 等已知运行事实保留为 observation/metadata，
不伪装成质量 Score。跨多个 observation 的 `tool_use` 轨迹判断第一阶段只进入 Codex 报告。Experiment
task-level Score、Dataset 和运行契约统一由 [`evals/README.md`](../evals/README.md) 定义，本文件不复制。

Langfuse 日常 evaluator 使用原生 **Live Observations** 和 observation name/type filter；首次创建
默认 100% sampling，之后 `enabled/sampling` 由 Langfuse UI 作为运维状态管理。仓库 reconcile 只更新
evaluator reference、target、filter 和 mapping，不覆盖 UI 中的启停或采样；prompt、output definition 或
model connection 漂移时创建同名 evaluator 新版本。五个 evaluator family 除服务日常评分外，response
quality 与 grounding 还被两条 Dataset-ID 过滤的 Experiment Rule 复用。配置器只管理规则，不创建或启动
Dataset Experiment。不要新建 deprecated trace-level evaluator。
Langfuse 可按 `gen_ai.tool.name` 将 Tool execution SPAN 显示为
`shopping_search` 等具体工具名，因此 `tool_result_quality` 不依赖 observation name，而过滤 SPAN 且
metadata `assistant_agent.observation_kind=tool_execution`；该稳定标记由 canonical
`tool.finished/tool.failed` 在 OTel 投影边界生成。runtime audit 使用同一标记，并为迁移前 trace 兼容读取
nested `assistant_agent.canonical_event`，不枚举内置、MCP 或 Plugin 工具名。
`memory_extraction` 过滤 SPAN `memory.turn_ingestion`，回答、grounding 与 memory recall 过滤
GENERATION `llm.chat` 且 metadata `assistant_agent.runtime_action=text`；该 generation 的 input 同时包含
当前请求、上下文、可用工具结果和长期记忆，比只看根 SPAN 更适合 observation-level 语义判断。
OTel 普通 span attribute 在 Langfuse 中只进入不可直接筛选的 `metadata.attributes`；因此
`runtime_action` 与 `memory_semantic_evidence` 还必须通过
`langfuse.observation.metadata.assistant_agent.*` 显式投影为顶层 observation metadata，供上述 rule
命中。generic `assistant_agent.*` 属性继续保留用于原始 OTel 诊断。
必须先在 UI preview 核对 input/output mapping；Mem0 change text 还要求 operator 显式允许本机 memory
trace content。Evaluator/rule 公共 API 当前仍标为 unstable，因此仓库不自动改写 Langfuse 配置，避免
定时审计获得管理权限或随版本漂移破坏现有 evaluator。配置依据见 Langfuse 官方的
[LLM-as-a-Judge](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge) 与
[observation evaluator 排障](https://langfuse.com/faq/all/observation-eval-not-executing)。

本机当前版本已暴露 unstable evaluator/rule API，因此仓库提供带双重 apply gate 的一次性配置入口：

```bash
# 只读查看将创建的 canonical evaluator/rule
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_runtime_audit.py \
  configure-evaluators --model-provider qwen-judge --model qwen-flash

# operator 确认该 LLM connection、100% sampling 与费用后才执行
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_runtime_audit.py \
  configure-evaluators --model-provider qwen-judge --model qwen-flash \
  --apply --allow-online-judge
```

Evaluator 与 Live Observation Rule 使用同一个 `assistant_agent.quality.*` canonical 名称。入口创建
缺失的五条日常 Rule；新回归 Dataset 尚不存在时，两条 Experiment Rule 会明确列为 skipped，不影响
Live Judge 首次启用；Dataset 由审核后的失败 Score 创建后，再次运行配置器即按真实 Dataset ID 补齐规则。
若检测到早期 `assistant-agent-live-*` Rule 或错误版本创建的
`assistant_agent.quality.*.live` Rule，则通过同一 Rule ID 原地迁移为 canonical 名称，不删除或回写
历史 Score。`memory_extraction` Rule 额外过滤
`assistant_agent.memory_semantic_evidence=available`，因此只有
显式启用本地 memory trace content 且 observation 同时包含原对话和 Mem0 changes 时才调用 Judge。

`scripts/run_runtime_audit.py` 无论使用 `run` 还是一次性 `configure-evaluators`，都不负责启动 Langfuse、
写入 Dataset 或运行 Experiment；前者只读审计已有 Trace，后者配置 Live 与 Runtime Regression
Experiment Rules。失败 Score 的审核沉淀与 Experiment 启动见
[`evals/README.md`](../evals/README.md#日常失败到-runtime-regression)。

每日调度使用仓库提供的 user unit 模板；安装/启用属于 operator 动作，不由审计器自行修改。unit
必须链接到已部署的主 checkout（默认 `%h/pycharm_project/assistant_agent`），不得链接到临时 worktree。
首次安装或发现现有 user unit 指向旧 checkout 时，在主 checkout 中重新链接并启用 timer：

```bash
audit_project_root="$HOME/pycharm_project/assistant_agent"
systemctl --user link "$audit_project_root/deploy/systemd/user/assistant-agent-runtime-audit.service"
systemctl --user link "$audit_project_root/deploy/systemd/user/assistant-agent-runtime-audit.timer"
systemctl --user daemon-reload
systemctl --user enable --now assistant-agent-runtime-audit.timer
```

更新 unit 后只需 reload/restart timer，不手工启动 service；用 `systemctl --user list-timers
assistant-agent-runtime-audit.timer` 查看下次运行，用
`journalctl --user -u assistant-agent-runtime-audit.service` 查看状态和 artifact path。unit 默认工作目录为
`%h/pycharm_project/assistant_agent`、Python 为 `%h/miniconda3/envs/hello_agent/bin/python`；路径不同
必须先复制模板并显式修改。service 不对整次 daily run 设置有限 `TimeoutStartSec`；每次 Codex 子进程
仍保留自己的显式 timeout。

历史 `agent_eval.dimension.*` 和默认关闭的 legacy runtime Score 不做破坏性清理；它们仅作为历史数据
保留。`configure-evaluators --apply` 会把旧 `assistant-agent-live-*` 规则以及错误版本创建的
`assistant_agent.quality.*.live` 规则原地改回上述 canonical 名称，避免重复日常评分；它不会自动删除
错误版本可能创建的无 Dataset-ID 边界 `*.experiment` 规则，operator 应在 Langfuse UI 中确认后禁用或
删除。当前 Runtime Regression 规则固定使用 canonical evaluator family 与新 Dataset ID。

## 长期不变量

1. **单一运行时事实源**：Gateway、API、CLI、demo 和 eval 复用 Assistant Runtime；观测层不实现第二个 Agent loop。
2. **关联身份连续**：入口提供的 `run_id` 必须贯穿 Gateway、Runtime、summary 和可用的 delivery/latency 记录。
3. **span 因果真实**：started/terminal 共享 span 身份；未观察到终态时不伪造 finished span。
4. **wall time 不被嵌套 latency 替代**：Provider/Tool 自报 latency 只作诊断，不重复计入 critical path。
5. **发送、ACK 与任务终态分离**：`completed`、`sent`、`acked` 各自证明不同边界。
6. **prompt-safe 摘要**：turn summary、delivery audit、Gateway lifecycle 和 operational log 不含对话正文或原始 payload。
7. **内容与历史隔离**：本地调试 overlay 不进入 conversation history，failed turn 也不例外。
8. **敏感内容永不捕获**：credentials、authorization、hidden reasoning 和 inline binary media 被排除。
9. **观测 fail-open**：persistence、export、logging 或 score 失败不改变业务结果，同时诊断必须承认证据缺口。
10. **派生视图不反写事实**：结构化查询、metrics、Langfuse 和 grader 不改变 canonical event 的语义或 Runtime 状态。

`tests/core/contract/test_observability_contract.py` 保护已经登记的稳定观测契约；具体 core invariant 归属以
`tests/core/INVARIANTS.md` 为准。

## 实现与验证入口

| 路径 | 职责 |
| --- | --- |
| `src/assistant_agent/observability/trace_store.py` | canonical event、store protocol、redaction 和 debug summary |
| `src/assistant_agent/observability/trace_persistence.py` | server primary/secondary store、后台 JSONL 与 bounded close |
| `src/assistant_agent/observability/trace_query.py` | read-only run/trace/tool/context summary |
| `src/assistant_agent/observability/turn_summary.py` | `assistant_turn_summary_v2` |
| `src/assistant_agent/observability/agent_service_delivery.py` | delivery registry 与 `agent_service_delivery_v1` audit |
| `src/assistant_agent/observability/agent_service_latency.py` | `agent_service_turn_latency_v2` 和 critical-path 分析 |
| `src/assistant_agent/observability/trace_content_policy.py` | 本地 content/protocol capture 开关 |
| `src/assistant_agent/observability/trace_conversation.py` | 有界、进程内 current-turn content overlay |
| `src/assistant_agent/observability/otel_mapping.py` | canonical trace 到 OTel/Langfuse span plan 的映射 |
| `src/assistant_agent/media/vision/observability.py` | 内容安全的 `vlm.infer` generation 事件边界 |
| `src/assistant_agent/observability/operational_logging.py` | Gateway console、JSONL 和兼容 text log |
| `src/assistant_agent/gateway/observability.py` | prompt-safe Gateway lifecycle schema 和 sink |
| `tests/core/contract/test_observability_contract.py` | 稳定 observability core contract |

真实运行诊断命令、证据优先级和降级路径集中维护在
[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md)，不在本文件重复。

## 更新规则

修改 trace event、summary schema、ID 传播、persistence、redaction、content capture、Gateway lifecycle、
delivery audit、latency 归因或 OTel/Langfuse projection 时，必须：

1. 先修改 owning source 和相应测试；
2. 更新本文档中的稳定语义和边界，而不是追加开发过程；
3. 若诊断入口或证据顺序变化，同步更新 diagnosis runbook；
4. 若脚本参数变化，只在 runbook 保留仍有操作价值的典型命令，完整列表继续由 `--help` 提供；
5. 将一次性设计、迁移步骤和阶段计划保留在 `docs/development/**` 或 `docs/superpowers/**`，不回填当前权威。

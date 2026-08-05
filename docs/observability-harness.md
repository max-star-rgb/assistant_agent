# Observability Harness

Last updated: 2026-08-05

本文档是 `assistant_agent` 当前 observability 架构、trace 语义、日志边界与
redaction 规则的权威入口。它定义系统必须保留的稳定机器事实和各观测面的职责，不复制
Pydantic 字段全集、脚本参数全集或外部 UI 的展示细节。

真实测试、真实通话、真实 run/trace 或机器日志的具体定位步骤见
[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md)。评估数据集、Experiment
和 grader 的职责边界见 [`../evals/README.md`](../evals/README.md)。

## 定位与权威边界

Observability 是运行时行为的只读投影，不是另一套执行状态机：

- Assistant Runtime、Gateway、Tool、Memory 和 Provider 各自拥有业务状态；观测层记录它们已经发生的事实。
- canonical trace 是 Assistant run 内部决策、阶段、工具、错误和终态的详细诊断来源。
- `assistant.turn.summary` 是一个 turn 的身份与终态摘要；摘要不能覆盖更细粒度的 raw timeline 事实。
- Gateway lifecycle 和 Agent-Service delivery audit 分别证明入口生命周期与媒体发送/ACK 状态；它们不能由
  Assistant terminal status 推导。
- Langfuse、OpenTelemetry、开发者 viewer 和评估分数都是 canonical trace 的投影或派生视图，不得成为冲突的第二事实源。

发生冲突时按以下顺序判断：当前源码和测试高于 prose；同一运行中，原始 machine event 高于派生摘要，
派生摘要高于 viewer 文案；Git 历史只用于解释演进，不证明当前行为。

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
| `trace_id` | 一个 Assistant trace 的公共查询键；也是 Langfuse `assistant.turn` trace 的关联键 |
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
同一 observer 还发布 `visual_semantic.retained/evicted/index_failed` 与 `visual_memory.query`；只记录
哈希化 session、sequence、稳定 status、数量和 latency，不记录 VLM 文本、用户 query、向量或 evidence。

视觉上下文预算事件为 `visual_context.preflight/compacted/compaction_failed/hard_limit`：真实
`VisualContextService` 在初始预算评估、成功 CAS 后重建、compactor 失败和最终 hard 拒绝处分别发出；
未启用 compaction 的 realtime fallback 也发出 status=`unavailable`、compacted=`false` 的 preflight。
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
到成功语义发布的端到端耗时；它是后台视频诊断，不与 chat critical path 重复相加。

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

- 根 span 名为 `agent.runtime`，Langfuse trace 名固定为 `assistant.turn`；
- ReAct iteration 是根 span 下的逻辑 span；
- 声明了 observation type 的 operation 映射为 span、generation 或 event；
- operation 的 parent、start/end、status 来自 canonical span 关系和 started/terminal event；
- usage 映射到 generation/OTel token attributes，不能因嵌套结构而丢失；
- only-allowlisted metadata 和 output reference 可以进入公开 projection。

`assistant.turn.summary` 到达时主 trace 可以先导出；后续后台 `memory.ingestion.finished` 不得被
静默丢弃。OTel observer 将它作为单独的 late `memory.turn_ingestion` span 追加到同一 trace，
parent 使用稳定的 `agent.runtime` root span ID。已有 `langfuse.session.id` 负责在 Session 页面聚合
同一会话的多个 turn；Langfuse 不创建第二套 session 或 memory 数据库。

Langfuse 的 trace、observation、score 和 Dataset/Experiment 是远端投影与评估记录。面板如何折叠或展示
长 JSON 不属于架构契约；需要完整证据时查询 observation 数据或回到 canonical trace。

### Langfuse-first Runtime 审计

`scripts/run_runtime_audit.py` 提供只读的日常审计入口。它默认重扫最近两小时的全部
`assistant.turn`，从 Langfuse 读取完整 trace、observation 和 Score；本地
`.data/graph_trace.jsonl` 只生成 trace/run、时间、terminal 与 event count 完整性 manifest。只有
Langfuse API 可读但缺少对应 trace 时，才把该 trace 的有界、redacted local timeline 放入 fallback
evidence。Langfuse 本身不可读时只记录 infrastructure unknown，不能把全部本地 trace 误报为导出缺失。

产物固定写入 `.data/runtime_audit/`：

- `state/watermark.json`：最近窗口与 bundle 路径；
- `inbox/<audit_run_id>.json`：版本化、只读 audit bundle；
- `reports/<audit_run_id>.json`：可选 Codex 结构化报告；
- `reports/<audit_run_id>.md`：确定性基线或经 schema 校验的 Codex 人工审阅报告。

Codex 通过 `--ephemeral --sandbox read-only` 运行；子进程环境移除 Langfuse 和 Provider credentials，
强制 mock Provider mode，只允许生成报告，不允许修改代码、Langfuse 或 Memory。Langfuse 读取失败、
Judge pending 和 evaluator 基础设施失败都不属于质量失败。

统一质量 Score 名称为：

| Score | 目标与来源 |
| --- | --- |
| `assistant_agent.quality.response_quality` | 最终文本 `llm.chat` generation 的回答质量；日常使用原生 observation LLM-as-a-Judge，Experiment 可由受控 grader 写入 |
| `assistant_agent.quality.grounding` | 最终文本 generation 对工具/上下文证据的忠实度；日常 observation evaluator，Experiment 可由受控 grader 写入 |
| `assistant_agent.quality.tool_result_quality` | 单个 `tool.execute` observation 的结果语义质量；只使用 observation evaluator |
| `assistant_agent.quality.memory_extraction` | 单个 `memory.turn_ingestion` observation 的长期记忆提取质量 |
| `assistant_agent.quality.memory_recall` | 具有实际召回证据的 memory/LLM observation 的召回质量；证据不足时保持 missing/unsupported，不伪造失败 |
| `assistant_agent.quality.task_conformance` | 仅 Experiment；Environment oracle 与 Mission objective Rule 的任务符合度 |

Score name 只表达测量对象；`source`、judge/model、evaluator version、live/experiment mode 放 Score
metadata。terminal、Tool 调用成败、event count、latency 等已知运行事实保留为 observation/metadata，
不伪装成质量 Score。跨多个 observation 的 `tool_use` 轨迹判断第一阶段只进入 Codex 报告。

Langfuse 日常 evaluator 使用原生 **Live Observations**、100% sampling 和 observation name/type filter；
不要新建 deprecated trace-level evaluator。`tool_result_quality` 过滤 SPAN `tool.execute`，
`memory_extraction` 过滤 SPAN `memory.turn_ingestion`，回答、grounding 与 memory recall 过滤
GENERATION `llm.chat` 且 metadata `assistant_agent.runtime_action=text`；该 generation 的 input 同时包含
当前请求、上下文、可用工具结果和长期记忆，比只看根 SPAN 更适合 observation-level 语义判断。
必须先在 UI preview 核对 input/output mapping；Mem0 change text 还要求 operator 显式允许本机 memory
trace content。Evaluator/rule 公共 API 当前仍标为 unstable，因此仓库不自动改写 Langfuse 配置，避免
定时审计获得管理权限或随版本漂移破坏现有 evaluator。配置依据见 Langfuse 官方的
[LLM-as-a-Judge](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge) 与
[observation evaluator 排障](https://langfuse.com/faq/all/observation-eval-not-executing)。

本机当前版本已暴露 unstable evaluator/rule API，因此仓库提供带双重 apply gate 的一次性配置入口：

```bash
# 只读查看将创建的 canonical evaluator/rule
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_runtime_audit.py \
  configure-evaluators --model-provider deepseek-judge --model deepseek-v4-flash

# operator 确认该 LLM connection、100% sampling 与费用后才执行
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_runtime_audit.py \
  configure-evaluators --model-provider deepseek-judge --model deepseek-v4-flash \
  --apply --allow-online-judge
```

入口只创建缺失的 project evaluator/rule，已存在项保持不动；不会删除或隐式升级历史 evaluator
或 Score。`memory_extraction` rule 额外过滤 `assistant_agent.memory_semantic_evidence=available`，因此只有
显式启用本地 memory trace content 且 observation 同时包含原对话和 Mem0 changes 时才调用 Judge。

每小时调度使用仓库提供的 user unit 模板；安装/启用属于 operator 动作，不由审计器自行修改：

```bash
systemctl --user link "$PWD/deploy/systemd/user/assistant-agent-runtime-audit.service"
systemctl --user link "$PWD/deploy/systemd/user/assistant-agent-runtime-audit.timer"
systemctl --user daemon-reload
systemctl --user enable --now assistant-agent-runtime-audit.timer
systemctl --user start assistant-agent-runtime-audit.service
```

用 `systemctl --user list-timers assistant-agent-runtime-audit.timer` 查看下次运行，用
`journalctl --user -u assistant-agent-runtime-audit.service` 查看状态和 artifact path。unit 默认工作目录为
`%h/pycharm_project/assistant_agent`、Python 为 `%h/miniconda3/envs/hello_agent/bin/python`；路径不同
必须先复制模板并显式修改。

历史 `agent_eval.dimension.*` 和默认关闭的 legacy runtime Score 不做破坏性清理；它们仅作为历史数据
保留。新 Experiment 与日常 evaluator 必须使用上述 canonical 名称，迁移后的完整性检查也只认新名称。

text turn score、trajectory diagnostic 和 Agent Experiment 都必须从已记录事实派生。grader 的真实调用、
Dataset 发布和 Experiment 运行由 [`../evals/README.md`](../evals/README.md) 管理；不得因 observability
默认开启而自动调用真实 Provider 或 judge。

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
10. **派生视图不反写事实**：viewer、metrics、Langfuse 和 grader 不改变 canonical event 的语义或 Runtime 状态。

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
| `src/assistant_agent/observability/operational_logging.py` | Gateway console、JSONL 和兼容 text log |
| `src/assistant_agent/gateway/observability.py` | prompt-safe Gateway lifecycle schema 和 sink |
| `scripts/agentruntime_view.py` | canonical runtime trace 开发者 viewer |
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

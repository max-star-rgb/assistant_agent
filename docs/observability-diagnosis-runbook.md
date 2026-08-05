# 真实运行诊断 Runbook

Last updated: 2026-07-31

本文档用于诊断真实测试、真实通话、真实 run/trace 和机器日志。它回答“拿到一次真实运行后如何取证”，
不重新定义 trace schema 或运行时契约；事件和安全边界以
[`observability-harness.md`](observability-harness.md) 为准。

## 诊断原则

1. **先查本次运行的机器事实**：不先用 mock 复现、经验判断、旧 trace 或源码推演替代当前运行。
2. **精确标识，分层取证**：`trace_id`、`run_id`、`turn_id`、`delivery_id` 各有职责，必须先确认关联。
3. **远端与本地互补**：Langfuse 证明已导出的 trace；本地 canonical trace、Gateway lifecycle 和
   delivery audit 补充未导出、入口和交付事实，任一方不必等另一方命中后才开始检查。
4. **raw event 优先于派生文案**：summary 和 viewer 用于导航；因果、未闭合 span 和部分失败回到 timeline。
5. **承认证据缺口**：观测面 fail-open，查不到不等于没有发生。结论必须区分机器事实、源码解释和推测。

诊断过程中不得输出 Langfuse secret、API key、Authorization header、用户原文、Provider 原始 payload、
Memory 内容或媒体。只有排查确实需要正文时，才在 loopback server 上显式查询当前 turn 内容。

## 收到 `assistant.turn: <trace_id>` 时

用户提供 `assistant.turn: <32 位十六进制 ID>` 时，默认把该 ID 当作当前环境 Langfuse
`assistant.turn` trace 的 `trace_id`，不是 observation ID、run ID 或自由文本搜索词。

立即执行两条互不阻塞的取证路径：

1. 使用本机未跟踪配置中的 Langfuse host 和凭据，精确查询
   `GET /api/public/traces/{trace_id}`；默认本地 host 是 `http://localhost:3000`。凭据只用于认证，
   不得打印或写入文档。
2. 用同一 ID 检查 `.data/graph_trace.jsonl`，并按问题类型检查 Gateway lifecycle、delivery audit
   和相关 eval artifact。

开始归因前，核对：

- 返回的 trace ID 与用户提供值完全一致；
- Langfuse trace 名是 `assistant.turn`；
- timestamp 与用户描述的测试时间、时区和环境一致；
- metadata 或本地事件中的 `run_id`、`session_id`、`turn_id` 属于同一次运行；
- 若用户提供了多个标识，它们确实通过 machine record 关联，而不是仅凭时间接近。

精确 trace 命中后，不要求用户再提供 run ID 或 session ID。若当前 Langfuse 不可达、无权限或查无此
ID，再按本地证据降级；两边均无法定位时才请求环境、host 或时间范围。

## 快速入口

### 查看 Langfuse trace

日常人工诊断先在本机 Langfuse 的 `assistant.turn` 中按精确 `trace_id` 查询，读取 observation、
input/output、status、usage 和 Score。Langfuse 是人工查看主入口，但 UI 未命中只代表远端证据缺失，
不能据此断言 Runtime 未执行。

### 查询 loopback server

当前 HTTP 查询面：

```text
GET /runs/{run_id}
GET /traces/{trace_id}
GET /runs/{run_id}/tool-calls
GET /traces/{trace_id}/context
GET /traces/{trace_id}/conversation  # 仅显式本地内容诊断
```

Server 仍在运行时，可直接读取结构化 trace：

```bash
curl -fsS "http://127.0.0.1:8089/traces/<trace_id>" \
  | /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m json.tool
```

只有确需当前 turn 正文且 Server 使用 `--allow-local-trace-content` 启动时，才查询 loopback 内容接口：

```bash
curl -fsS "http://127.0.0.1:8089/traces/<trace_id>/conversation" \
  | /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m json.tool
```

非 loopback 请求会被拒绝。正文只用于当前问题的本地诊断，不应粘贴进 issue、日志或最终报告。

### 检查 Gateway 与 delivery JSONL

Langfuse 或 Server 不可用时，使用关联 ID 直接检索 prompt-safe 本地机器 JSONL：

```bash
rg -n --fixed-strings '<run_id>' \
  .data/gateway_events.jsonl \
  .data/graph_trace.jsonl \
  .data/agent_service_delivery.jsonl

rg -n --fixed-strings '<trace_id>' \
  .data/gateway_events.jsonl \
  .data/graph_trace.jsonl \
  .data/agent_service_delivery.jsonl
```

文件不存在时单独检查现有文件，不把 `rg` 的 missing-file 错误解释为运行失败。Gateway JSONL 中
`session_id` / `user_id` 是摘要，不能用原始身份直接搜索；优先用 `run_id`、`turn_id` 或 `trace_id`。

## 标准诊断流程

### 1. 确认运行身份与环境

记录但不泄露以下信息：

- trace ID、run ID 和可用的 turn/delivery ID；
- trace timestamp、用户报告时间及 timezone；
- client type、入口类型、provider mode 和部署/本地环境；
- 数据来源：Langfuse、本地 trace、Gateway JSONL、delivery JSONL 或 eval artifact。

任一关键身份不一致时先停止行为归因。相邻运行、重试运行和同一 session 的下一 turn 不能混为一条 trace。

### 2. 先读 terminal summary

优先定位最新 `assistant.turn.summary`，读取 terminal、entry、runtime、response、tool/error count 和可选
latency reference。它用于快速回答：

- Runtime 是否已结束；
- entry 是否失败但 runtime 仍处于 pending cancel；
- 是否生成了响应；
- 是否存在 Tool 或结构化错误；
- Agent-Service latency 摘要是否可关联。

旧 trace 没有 summary 时，再使用 `run.completed`、`run.failed`、`run.cancelled` 和
`agent_service.turn.finished` 推断当前已知终态，并明确标注这是兼容降级。

### 3. 检查 timeline 和 span 闭合

按时间检查：

1. run/entry 是否开始；
2. conversation、memory、context 是否完成；
3. 每个 `llm.chat.started` 是否有共享 span ID 的 terminal；
4. decision 是否产生 Tool call、text、refusal、truncated、empty 或 error；
5. Tool validation、started、finished/failed 是否成对；
6. phase/guard 是否改变后续执行；
7. response finalize、postprocess 和 run terminal 是否存在。

started 没有 terminal 只说明 span 未闭合或证据不完整。结合最后事件、cancel/timeout 和 persistence 状态，
不要自动判定为该组件崩溃。

### 4. 分析 latency

优先使用 wall time 和 `agent_service_turn_latency_v2` 的 critical-path stages：

- 找最大 critical-path stage；
- 比较 LLM wall latency 与 Provider-reported latency，但不相加；
- 比较 Tool executor wall latency 与 tool-reported latency，但不相加；
- 检查正 `unattributed`，不要默认归因给 Provider；
- 将 ACK latency、视频 freshness 和 Provider 内部 latency 视为次级诊断。

若摘要在 timeout 时生成，`active_stage` 只表示截止 deadline 最近的未闭合 started span；最终状态仍等待
后续 `run.cancelled` / `run.failed` 事件确认。

### 5. 检查入口与交付

只有问题涉及“没收到、断开、排队、取消、ACK 或发送慢”时才继续查入口面：

- Gateway lifecycle：session、queue、admission、run、cancel、interrupt、terminal；
- delivery audit：accepted、processing、sent、acked、failed、interrupted 或 disconnect；
- latency summary：WebSocket send 边界、ACK state 和 failure source。

解释状态时保持边界分离：

| 机器事实 | 能证明 | 不能证明 |
| --- | --- | --- |
| `run.completed` | Assistant Runtime 完成 | 客户端收到或处理响应 |
| `sent` | `send_text()` 已返回 | 媒体应用已处理响应 |
| `acked` | 协商 ACK 后媒体应用确认 | 回答语义正确 |
| `failed` delivery | 交付路径失败 | Runtime 必然失败 |
| `disconnected_before_ack` | send 后、ACK 前断开 | 消息一定未被用户看到 |

### 6. 最后用源码解释

机器事实确定后，再按 owning module 解释原因：

- Runtime/phase/loop：`src/assistant_agent/runtime/assistant_loop_nodes.py`；
- Tool lifecycle：`src/assistant_agent/runtime/event_publisher.py` 与 Tool 治理链；
- trace/summary：`src/assistant_agent/observability/`；
- Gateway lifecycle：`src/assistant_agent/gateway/` 与 `src/assistant_agent/api/gateway_*`；
- Agent-Service delivery：`src/assistant_agent/api/agent_service_websocket.py`。

源码可以解释“观察到的事件为何按这种规则产生”，不能证明一次缺失 trace 中实际走过了某条分支。

## 分层诊断矩阵

| 症状 | 先看 | 关键问题 |
| --- | --- | --- |
| 请求未进入执行 | Gateway lifecycle | 是否 admission/queue rejected、session closed 或未创建 run |
| run 卡住或超时 | turn summary + raw timeline | 最后闭合 span 是什么，是否 pending cancel，是否有后续 terminal |
| Provider 慢或失败 | `llm.chat` spans | wall/provider latency、attempt kind、error、usage、finish/result kind |
| Tool 未调用 | decision + validation/guard | 模型是否提出 Tool call，是否被 validator/guard 阻止 |
| Tool 调用失败或重复 | Tool span + observation | validation、attempt、retry、terminal 和 tool call ID 是否一致 |
| 回答为空或降级 | LLM result + phase change + response final | empty/error/truncated/refusal、FINALIZE、protocol violation 或 guard |
| Memory/Context 异常 | memory/context spans + context report | 是否加载、预算、裁剪、错误或 fallback；不读取原始 memory 内容 |
| 用户没收到响应 | delivery audit + Gateway lifecycle | Runtime terminal、sent、ACK 和 disconnect 分别是什么 |
| 整体慢 | latency summary + raw paired spans | critical-path bottleneck、嵌套 latency 和 unattributed 各是多少 |
| Langfuse 与本地不同 | export/persistence 状态 | 是否异步未导出、JSONL 部分写入、进程内事件尚未落盘或 ID/环境不一致 |

## 证据缺失时的降级

### Langfuse 不可达、无权限或查无 trace

- 记录缺失的是远端持久化证据，而不是断言 trace 不存在；
- 按精确 trace/run ID 查询 `.data/graph_trace.jsonl`、Gateway 和 delivery JSONL；
- server 仍存活时用 `--server` 查询进程内 primary；
- 本地命中后可以诊断本地事实，但不能声称远端 export 成功。

### 本地 JSONL 未命中

- 检查路径和启动参数是否使用默认 `.data/graph_trace.jsonl`；
- server 仍存活时查询进程内 primary；
- 检查 Langfuse exact trace；
- 考虑后台队列 drop、崩溃或 bounded flush 造成的部分持久化；
- 不能因 JSONL 未命中断言 Runtime 从未运行。

### 只有 partial trace 或未闭合 span

- 记录最后一个 machine event、最后闭合 stage 和所有 open span；
- 检查 cancel、interrupt、deadline、disconnect 和后续 terminal；
- 区分“截止查询时仍在退出”和“已经证明失败”；
- 不人工补造 `*.finished`，不拿 `latency_ms` 覆盖真实 start time。

### 身份或时间不匹配

- 停止归因，不把相邻 run 当成当前问题；
- 用 run/turn/delivery ID 重新关联；
- 必要时请求用户补充环境、Langfuse host、测试时间和 timezone；
- 不要求用户提供 credentials 或原始敏感 payload。

### Conversation 不可用

- 继续使用 prompt-safe overview、decision 和 timeline；
- 检查是否关闭了 local trace content、server 是否为 loopback、进程是否已重启；
- 只有语义判断确实需要正文时才请求最小必要用户片段；
- 不用 conversation history 猜测失败 turn 的最终输出。

## 形成诊断结论

回答真实运行问题时使用以下结构：

```text
定位：trace=<trace_id>，run=<run_id>，时间=<timestamp + timezone>，来源=<Langfuse/本地文件>

机器事实：
- 按时间列出决定性事件、状态和 latency。

源码解释：
- 说明这些事件对应的当前控制流或契约。

结论：
- 直接原因、影响边界和是否已经终止。

限制：
- 缺失的观测面、未闭合 span、无法确认的内容。

下一步：
- 最小补证或修复建议。
```

避免使用“应该是”“大概率”掩盖证据缺口。确实只能推测时，明确写出推测依据以及能证伪它的下一条机器证据。

## Runbook 更新规则

- viewer、query API、默认文件路径、Langfuse 查询方式或诊断证据顺序变化时更新本文件。
- event/schema 语义变化时先更新 [`observability-harness.md`](observability-harness.md)，本文件只调整操作步骤。
- 典型命令必须在当前脚本 `--help` 中存在；完整参数列表不复制到本文档。
- 不加入个人端口、真实凭据、某次事故 payload 或已经完成的开发 Phase Plan。

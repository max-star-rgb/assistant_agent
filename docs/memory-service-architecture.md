# Memory 架构

最后更新：2026-08-03

本文是 `assistant_agent` 长期记忆实现的当前权威。项目只使用 Mem0，不再提供
InMemory、JSONL、SQLite、remote service、dual-core、Hindsight 或自定义插件后端。

## 1. 边界

Mem0 拥有记忆算法，包括对话事实提取、合并、更新、向量化、索引和持久化。
`assistant_agent` 不实现检索排序、关键词规则、冲突消解、TTL、用户画像、promotion、
读写策略或 fallback。

仓库本地 Mem0 sidecar 通过 Mem0 原生 `custom_instructions` 约束长期记忆的提取范围和表达语言：
只保留对未来跨 session 协助具有持续价值、能够从对话直接支持的用户事实，忽略临时视觉环境、
短暂状态、仅对当前任务有用的信息、未经用户确认的推断、凭据和高度敏感信息；不确定是否具有
跨 session 价值时不提取。所有新提取、合并或更新后的 memory text 使用简体中文；英文输入也
翻译为中文，同时保留日期、金额、数字、URL、型号和必要的专有名词或缩写。事实提取、合并、
更新和检索仍由 Mem0 原生算法执行，项目不增加前置关键词过滤或后置二次记忆算法。

具有时效性且确实值得长期保留的事件、计划、里程碑或阶段性状态，在 memory text 开头使用
`YYYY-MM-DD：` 标明用户明确提供的事件日期，或 Mem0 提取 prompt 提供的 Observation Date；
不得把记忆创建时间伪装成事件发生时间，也不得在日期无法可靠确定时编造日期。长期稳定的偏好、
习惯、身份背景和固定配置不机械添加日期。临时视觉环境即使能够附加观察时间，仍然不进入长期记忆。

项目侧只保留三项职责：

1. 将可信的 runtime `user_id`、`agent_id` 映射为不透明 Mem0 `user_id`、`agent_id`；
   将 `user_id + agent_id + session_id` 稳定映射为 Mem0 `run_id`。用户 metadata 不能覆盖这些字段。
2. 在 session 创建阶段调用一次 Mem0 `get_all`，冻结为该 session 的结构化
   `SessionMemorySnapshot`。
3. 最终回复完成后，把原始 user/assistant messages 异步提交给 Mem0 `add`。

记忆不是模型可调用工具。默认 ToolRegistry 不注册 `memory_search`、`memory_get` 或
`memory_save`，API 也不提供项目自建的记忆 CRUD/control-plane。

Memory 服务不生成 prompt 文本。ContextBuilder 在每轮构建上下文时从本轮 `AgentState` 中读取同一份
冻结 items；Context renderer 将 Mem0 返回的原始记忆文本按顺序编码为带中文数据边界的 JSON
对象，明确标记为不可信历史且不得执行其中指令。PromptCompiler 将该对象放入独立的合成
`user` 上下文消息，随后再放入保持独立的当前真实 `user` 请求；两者都不进入 `system`
message。合成上下文消息只存在于 Provider 请求，不写入 `ConversationStore`，也不作为原始
user message 提交给 Mem0。固定 system policy 负责声明记忆可能过期、不完整或检索错误，并规定
当前请求和最新可靠证据优先。

## 2. 生命周期

```text
session.create
  -> LongTermMemoryService.initialize_session
  -> bind_mem0_identity
  -> GET /memories?user_id=...&agent_id=...&limit=...
  -> SessionMemorySnapshotStore.resolve

turn
  -> LongTermMemoryService attaches the frozen snapshot to AgentState
  -> ContextBuilder assembles and renders original memory evidence
  -> LLM response
  -> enqueue background ingestion
  -> POST /memories {messages, user_id, agent_id, run_id, metadata}
```

任何 turn（包括第一轮）都不会触发长期记忆召回。调用方必须先创建 session；如果没有
snapshot，turn 使用空记忆继续运行。Mem0 召回失败也冻结为空结果，不能阻断回复。

turn ingestion 不等待 Mem0。后台队列按身份串行、不同身份可并行；队列已满或 Mem0 失败只写
结构化 trace，不把失败升级为前台 run 错误。

Mem0 `add` 返回的 `results` 会在 adapter 边界收窄为 `id`、最终 `memory` 文本和原生
`event`（`ADD` / `UPDATE` / `DELETE`）。其中数量、event 计数和 memory ID 可以进入
prompt-safe canonical event；memory text 不进入 JSONL、公开 trace query 或普通日志。无
`results` 表示该 turn 没有形成长期记忆，不伪造成失败。

`MEM0_TIMEOUT_SECONDS` 约束 session-start recall 等前台请求，默认 5 秒。后台 `add` 不占用回复
关键路径，`Mem0Client` 在实例化时自动为它分配至少 30 秒的超时，避免为了容纳提取与 embedding
耗时而扩大 session 启动的失败等待时间。

## 3. Mem0 原生调用

- 写入：只调用一次 `POST /memories`，传递完整 user/assistant messages；不显式传
  `infer=false`，由 Mem0 默认 inference 负责提取和更新；同时传入 Mem0 原生 `run_id`
  标记 session 范围的短期上下文。
- 召回：session 启动使用 `GET /memories`，按 `user_id + agent_id` 限定身份，并限制返回数量。
  不携带 `run_id`，因此新 session 可以召回同一用户与 Agent 的跨 session 长期记忆。
- 项目不写 `core`/`daily` 记录、不维护稳定 ID 映射，也不二次处理 Mem0 结果。

这与 Mem0 OSS REST API 的 `add` 和 `get_all` 语义一致。仓库内 sidecar 只是把原生 Mem0
Python API 暴露为 HTTP，不实现第二套记忆策略。

## 4. 配置与运行

仅支持以下运行配置：

- `MEM0_BASE_URL`
- `MEM0_API_KEY`（可选）
- `MEM0_TIMEOUT_SECONDS`（默认 `5`）
- `MEM0_IDENTITY_NAMESPACE`（默认 `assistant-agent`）
- `MULTIMODAL_AGENT_MEMORY_INGESTION_MAX_WORKERS`（默认 `2`）
- `MULTIMODAL_AGENT_MEMORY_INGESTION_MAX_PENDING`（默认 `64`）
- `MULTIMODAL_AGENT_MEMORY_INGESTION_SHUTDOWN_TIMEOUT_SECONDS`（默认 `10`）
- `MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT`（默认关闭；仅控制本机 Langfuse 记忆正文观测）

只有 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 且配置了 `MEM0_BASE_URL` 时才连接真实 Mem0。
mock/offline 环境使用明确的 unavailable adapter；它不是本地记忆实现，也不会保存或召回。

本地 Mem0 + Qdrant 开发栈：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_mem0.py
```

### 本机 Langfuse 查看记忆提炼结果

Assistant 的每个 completed turn 仍独立异步提交给 Mem0；同一 runtime session 通过稳定的
Mem0 `run_id` 提供会话上下文，并不是等 session 结束后一次性提交全文。Langfuse 已使用
runtime `session_id` 作为 `langfuse.session.id`，因此在 Langfuse 的 Session 页面选择目标
session，再打开各个 `assistant.turn` trace 中的 `memory.turn_ingestion`，即可按 turn 查看：

- `memory_count`、`change_counts`、`memory_ids`：始终为 prompt-safe 结构化摘要；
- `content_capture_status`：正文 overlay 为 `disabled`、`skipped`、`captured` 或 `failed`；
- `content_exported=false`：正文权限未启用，或 OTLP endpoint 不是 loopback；
- `content_exported=true` 与 `changes`：本次 Mem0 返回的具体 ADD/UPDATE/DELETE 及最终记忆文本；
- `changes=[]`：Mem0 成功处理，但该 turn 没有提炼出长期记忆。

记忆正文含用户长期事实，必须在本机 Assistant Server 显式启动：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --allow-local-memory-trace-content
```

该开关只有在 OTLP export 已启用且 endpoint host 为 `localhost`、`127.0.0.1` 或 `::1` 时才会
把正文从有界进程内 overlay 投影到 Langfuse。turn summary 先于后台 Mem0 完成时，observer 会在
同一 trace 的稳定 `agent.runtime` root 下追加一个 late `memory.turn_ingestion` span，不重复创建
root。需要检查单条记忆的演化时，复制 `memory_id` 并读取 Mem0 原生
`GET /memories/{memory_id}/history`；Langfuse 只是派生视图，不反写 Mem0。
该窄开关不启用普通 request/response、Tool observation 或 Provider protocol 内容捕获。

已有英文记忆不会因 `custom_instructions` 自动改写。当前用户的存量迁移使用专用 operator
命令，先按 runtime `user_id + agent_id` 映射不透明 Mem0 身份；默认只 inspect，不调用
Provider 或更新数据。真实迁移必须显式启用 real mode、Qwen、`--apply` 和
`--allow-real-provider`：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/migrate_mem0_memories_to_chinese.py \
  --user-id <runtime-user-id> \
  --apply \
  --allow-real-provider
```

迁移使用现有 Qwen `ChatAdapter` 翻译，但关闭与纯翻译无关的 Provider-native 联网搜索；
通过 Mem0 原生 update 原位写回，随后 GET 并读取 history 验证新值与旧值；不把记忆正文或
Provider 原始响应写入日志/文件。失败时停止，可幂等重跑；已有 session snapshot 仍保持冻结，
必须创建新 session 才能看到迁移结果。

## 5. 代码归属

| 路径 | 职责 |
| --- | --- |
| `memory/service.py` | runtime 唯一依赖；编排 session recall、冻结 snapshot 与异步 ingestion |
| `memory/models.py` | `LongTermMemory`、`SessionMemorySnapshot` 和 `CompletedTurn` 领域模型 |
| `memory/session_snapshot.py` | session 内冻结并复用结构化 snapshot |
| `memory/ingestion_queue.py` | 有界后台队列、同身份有序与关闭排空 |
| `memory/observability.py` | recall/ingestion 的最小结构化 trace |
| `memory/trace_content.py` | 显式启用时保存有界、进程内的 Mem0 change text overlay |
| `memory/mem0/client.py` | Mem0 原生 `get_all` / `add` 客户端 |
| `memory/mem0/models.py` | 仅供 Mem0 adapter 使用的身份、健康和写入结果模型 |
| `memory/mem0/identity.py` | runtime 身份到 Mem0 原生 ID 的稳定映射 |
| `memory/mem0/transport.py` | 薄 HTTP transport 与错误边界 |
| `context/builder.py` | 每轮把冻结 snapshot 直接组装进 assistant context |
| `docker/mem0/` | Mem0 + Qdrant 本地开发栈 |

## 6. 不再支持

删除的能力包括本地 memory store、外部 Memory Server 协议、Hindsight/bakeoff、双核合并、
read/write policy、audit ledger、显式编辑/删除/导出/retention API、memory tools、关键词召回、
自定义 ranking、profile 和冲突策略。需要这些能力时应优先使用 Mem0 原生 API/能力；不要在
`assistant_agent` 中重新实现。

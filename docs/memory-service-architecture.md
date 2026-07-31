# Memory 架构

最后更新：2026-07-25

本文是 `assistant_agent` 长期记忆实现的当前权威。项目只使用 Mem0，不再提供
InMemory、JSONL、SQLite、remote service、dual-core、Hindsight 或自定义插件后端。

## 1. 边界

Mem0 拥有记忆算法，包括对话事实提取、合并、更新、向量化、索引和持久化。
`assistant_agent` 不实现检索排序、关键词规则、冲突消解、TTL、用户画像、promotion、
读写策略或 fallback。

仓库本地 Mem0 sidecar 通过 Mem0 原生 `custom_instructions` 固定记忆文本的表达语言：
所有新提取、合并或更新后的 memory text 使用简体中文；英文输入也翻译为中文，同时保留日期、
金额、数字、URL、型号和必要的专有名词或缩写。该配置只约束表达语言，不接管 Mem0 的事实选择、
合并、更新或检索算法。

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

只有 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 且配置了 `MEM0_BASE_URL` 时才连接真实 Mem0。
mock/offline 环境使用明确的 unavailable adapter；它不是本地记忆实现，也不会保存或召回。

本地 Mem0 + Qdrant 开发栈：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_mem0.py
```

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

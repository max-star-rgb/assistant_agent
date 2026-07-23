# Memory 架构

最后更新：2026-07-23

本文是 `assistant_agent` 长期记忆实现的当前权威。项目只使用 Mem0，不再提供
InMemory、JSONL、SQLite、remote service、dual-core、Hindsight 或自定义插件后端。

## 1. 边界

Mem0 拥有记忆算法，包括对话事实提取、合并、更新、向量化、索引和持久化。
`assistant_agent` 不实现检索排序、关键词规则、冲突消解、TTL、用户画像、promotion、
读写策略或 fallback。

项目侧只保留三项职责：

1. 将可信的 tenant/user 映射为不透明 Mem0 `user_id`，将 tenant/project 映射为
   `agent_id`；session id 不进入长期记忆命名空间。
2. 在 session 创建阶段调用一次 Mem0 `get_all`，冻结为该 session 的 prompt snapshot。
3. 最终回复完成后，把原始 user/assistant messages 异步提交给 Mem0 `add`。

记忆不是模型可调用工具。默认 ToolRegistry 不注册 `memory_search`、`memory_get` 或
`memory_save`，API 也不提供项目自建的记忆 CRUD/control-plane。

## 2. 生命周期

```text
session.create
  -> bind_engine_identity
  -> GET /memories?user_id=...&agent_id=...&limit=...
  -> SessionMemoryContextStore.freeze

turn
  -> reuse frozen snapshot
  -> LLM response
  -> enqueue background capture
  -> POST /memories {messages, user_id, agent_id, metadata}
```

任何 turn（包括第一轮）都不会触发长期记忆召回。调用方必须先创建 session；如果没有
snapshot，turn 使用空记忆继续运行。Mem0 召回失败也冻结为空结果，不能阻断回复。

turn capture 不等待 Mem0。后台队列按身份串行、不同身份可并行；队列已满或 Mem0 失败只写
结构化 trace，不把失败升级为前台 run 错误。

## 3. Mem0 原生调用

- 写入：只调用一次 `POST /memories`，传递完整 user/assistant messages；不显式传
  `infer=false`，由 Mem0 默认 inference 负责提取和更新。
- 召回：session 启动使用 `GET /memories`，按 `user_id + agent_id` 限定身份，并限制返回数量。
- 项目不写 `core`/`daily` 记录、不维护稳定 ID 映射，也不二次处理 Mem0 结果。

这与 Mem0 OSS REST API 的 `add` 和 `get_all` 语义一致。仓库内 sidecar 只是把原生 Mem0
Python API 暴露为 HTTP，不实现第二套记忆策略。

## 4. 配置与运行

仅支持以下运行配置：

- `MEM0_BASE_URL`
- `MEM0_API_KEY`（可选）
- `MEM0_TIMEOUT_SECONDS`（默认 `5`）
- `MEM0_IDENTITY_NAMESPACE`（默认 `assistant-agent`）

只有 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 且配置了 `MEM0_BASE_URL` 时才连接真实 Mem0。
mock/offline 环境使用明确的 unavailable adapter；它不是本地记忆实现，也不会保存或召回。

本地 Mem0 + Qdrant 开发栈：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_mem0.py
```

## 5. 代码归属

| 路径 | 职责 |
| --- | --- |
| `memory/mem0/base.py` | Mem0 adapter 协议和身份映射 |
| `memory/mem0/adapters.py` | Mem0 OSS REST add/get-all adapter |
| `memory/mem0/store.py` | runtime 使用的薄适配 |
| `memory/manager.py` | session snapshot 与后台 capture 输入 |
| `services/session_memory_context.py` | session 内冻结和复用 snapshot |
| `services/memory_observability.py` | recall/capture 的最小结构化 trace |
| `docker/mem0/` | Mem0 + Qdrant 本地开发栈 |

## 6. 不再支持

删除的能力包括本地 memory store、外部 Memory Server 协议、Hindsight/bakeoff、双核合并、
read/write policy、audit ledger、显式编辑/删除/导出/retention API、memory tools、关键词召回、
自定义 ranking、profile 和冲突策略。需要这些能力时应优先使用 Mem0 原生 API/能力；不要在
`assistant_agent` 中重新实现。

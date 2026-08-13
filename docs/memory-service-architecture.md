# LangGraph 原生长期记忆架构

最后更新：2026-08-13

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 长期记忆 Graph 节点、冻结快照、后端装配和外部写入幂等的当前权威 |
| Owns | `MemoryNodeBundle`、`memory_recall` / `memory_commit`、`memory_context`、commit ledger、Mem0/LangMem/disabled 装配与 time-travel 语义 |
| Does not own | Mem0 私有 HTTP wire、conversation 编译、Gateway delivery ACK、通用 `BaseStore` 协议、模型可调用 Memory Tool |
| 源码与 schema 入口 | `src/assistant_agent/memory/`、`runtime/assistant_graph_state.py`、`runtime/assistant_loop_graph.py` |
| 验证入口 | `docs/authority.toml` 中 `graph-memory.verification`；核心不变量 `MEMORY-001` |
| 相邻 authority | Mem0 wire 见 [`memory_server_api_spec.md`](memory_server_api_spec.md)；Context 见 [`context_engineering_status.md`](context_engineering_status.md)；运行流见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md) |

## 1. 总体边界

长期记忆在运行流程中的统一抽象是 LangGraph `Node + State + Runtime`，不是项目自建 Memory SDK：

```text
Application composition root
  ├── Checkpointer
  ├── MemoryNodeBundle
  │    ├── recall_node
  │    ├── commit_node
  │    ├── optional BaseStore
  │    └── optional aclose
  └── Assistant StateGraph
       START
         ↓
       memory_recall
         ↓
       assistant / tool loop
         ↓
       publish_response
         ↓
       memory_commit
         ↓
        END
```

`MemoryNodeBundle` 是 frozen composition value，只允许
`backend_id / recall_node / commit_node / store / aclose`。它不提供 `search()`、`save()`、session、
policy、retry、health、queue 或 worker；新增能力应由明确 Graph node 表达，不能把 bundle 扩展成新的
Memory Service/Host。

一个 compiled Assistant graph 只装配一个 active backend。当前支持：

- `disabled`：完全离线，召回为空，写入跳过；
- `mem0`：节点直接调用同步 `Mem0Client`，不伪装成 `BaseStore`；
- `langmem`：manager 保留 extraction/update/merge 语义，Store 由
  `graph.compile(store=...)` 原生注入，节点从 `runtime.store` 读取。

assistant、tool 和 context compiler 不持有 backend/client/store 写引用。长期记忆写 authority 只在
`memory_commit`；未来若提供显式用户记忆命令，必须新增受治理的 `memory_command` 节点。

## 2. State 契约与冻结快照

`memory_recall` 将后端结果规范化为严格、版本化、checkpoint-safe 的 `MemoryContext`：

```text
backend_id / status / snapshot_id / items / issue_codes
item = memory_id / text / source / relevance? / updated_at?
```

`memory_context` 是一次 logical turn 看见的长期记忆“照片”。冻结后，同一 turn 的 assistant/tool
iteration 与 resume 都只消费 checkpoint 中的快照，不重新查询随时间变化的 backend。模型推理的稳定契约
只有有序 `text`；其余 item 字段仅用于 recall observability，业务逻辑不得依赖某个后端的 ID、score 或
时间字段。

正文限制为每项最多 4,000 字、总计最多 12,000 字、最多 32 项。后端返回文本始终是不可信历史数据，
不能覆盖 system/developer policy、当前用户请求、ToolSpec、身份或授权。Context compiler 仍负责最终
token budget 和 prompt-safe 数据边界。

## 3. 回答发布与写入

规范化最终回答形成后，Graph 先通过 `publish_response` 把稳定的 `RunFinalProductFact` 写入产品事件流，
再进入 `memory_commit`。Graph 等待 publish 调用成功，但不等待 Gateway/客户端 ACK：

```text
response.ready -> response.published -> memory_commit -> graph terminal
                                      ↘ response.delivered 可独立观测
```

`memory_commit` 只接收受信 runtime identity、规范化 user/assistant 文本和稳定 logical turn origin；
第三方原始异常与响应不得进入 State。commit 失败或超时只更新精简的 `MemoryCommitState`，不能改写已经
发布的回答，也不能令产品 run 失败。所有入口收到 final event 后仍须消费 Graph 至终态，不能提前停止。

## 4. 最小 commit ledger

LangGraph checkpoint 不能为外部 Memory API 提供 exactly-once，因此外部写入前必须经过独立
`MemoryCommitLedger`。稳定 `memory_event_id` 绑定：

```text
backend_id + logical turn origin + commit schema version + normalized input digest
```

SQLite 表 `memory_commit_events` 只记录 single-owner reserve、输入摘要和
`invoking / succeeded / failed / outcome_unknown`。`succeeded` 去重；无法证明结果的中断或 timeout 标记为
`outcome_unknown` 并 fail closed。ledger 不拥有 scheduler、retry、queue、worker、dead-letter 或 session
lifecycle，也不复用 Tool operation 的业务模型。

## 5. Time-travel 语义

| invocation | recall | memory snapshot | commit |
| --- | --- | --- | --- |
| new invoke | 查询一次 | 新建并冻结 | 允许一次 |
| resume | 不查询 | 复用中断 checkpoint | 原 product turn 以同一 event 去重 |
| replay | 不查询 | 继承所选历史快照 | 禁止 |
| default fork | 不查询 | 继承所选历史快照 | 禁止 |
| `refresh_memory=true` fork | 重新查询 | 新建非精确历史快照 | 默认仍禁止 |

精确 resume/replay/fork 缺少 `memory_context` 时 fail closed，不回退当前 backend。refresh fork 是受信请求的
显式 opt-in：它从 `memory_recall` 重新开始，因此不再是严格历史重现；其 `turn_provenance` 仍为
`time_travel`，所以 commit 被节点拒绝。

## 6. 配置与资源生命周期

受信配置 `MEMORY_BACKEND` 只能是 `disabled`、`mem0` 或 `langmem`，默认 `disabled`。mock mode 只能使用
disabled；远端 backend 必须同时满足 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 和完整显式配置，不能因发现
key 自动启用，也不能在配置失败时静默 fallback。

Mem0 使用 `MEM0_BASE_URL / MEM0_API_KEY / MEM0_TIMEOUT_SECONDS /
MEM0_IDENTITY_NAMESPACE`。LangMem 使用 `LANGMEM_MODEL` 选择记忆抽取模型，并复用当前受信
OpenAI-compatible Chat Provider 的 API key、base URL 与 timeout；不支持该协议的主 Provider 会在装配时
fail closed。它还要求显式 composition-owned `BaseStore` 和 optional dependency group
`memory-langmem`；缺包时启动失败并给出可解释配置错误。该 optional group 包含 HTTPX SOCKS transport，
composition root 会把标准 proxy 中的 `socks://` alias 规范化为 HTTPX 接受的 `socks5://`，且通过 bundle
`aclose` 关闭显式创建的同步/异步 client。

ledger 默认路径为 `.local/langgraph/memory_commits.sqlite3`，可通过 `MEMORY_COMMIT_LEDGER_PATH` 修改。
Store setup/migration/close 由 composition root 负责；client/manager 的可选异步关闭通过 bundle `aclose`
归还 composition root。Runtime 不维护 Memory session、后台 ingestion 或第二条生命周期。

## 7. Mem0 与 LangMem 特有语义

Mem0 recall 使用可信身份绑定后的 opaque `user_id + agent_id`；commit 额外携带 opaque `run_id` 和稳定
`source_turn=memory_event_id`。节点负责排序、裁剪、状态规范化和错误脱敏；事实提取、合并、更新、向量化
由 Mem0 完成。私有 HTTP 子集见 [`memory_server_api_spec.md`](memory_server_api_spec.md)。

LangMem manager 使用 `create_memory_store_manager`，namespace 为
`("assistant_agent", opaque_subject_id)`。recall 必须读取 compile 注入的同一 `runtime.store`；资源不一致
属于配置错误。项目不把 LangMem 或 Mem0 的专有 schema 暴露给其他 Graph 节点。

## 8. 可观测性

`memory_recall` 和 `memory_commit` 分别产生 `memory.recall.finished` 与
`memory.commit.finished` canonical event，并投影为同名 SPAN。只记录 backend ID、节点状态、召回项数、
裁剪后字符数、latency、memory event ID 与 issue code；不记录 Memory 正文、commit 对话、secret 或第三方
原始响应。观测写入 fail-open，不得改变 Graph 结果。

迁移前的 `memory.session_recall.finished`、`memory.ingestion.finished` 和 `memory.turn_ingestion` 只在
trace reader/exporter/evaluator 中保留历史数据兼容，不再由当前 Graph memory 主线产生。

## 9. 非目标

当前不提供多 backend 融合/双写、模型可调用 Memory Tool、自研通用 Memory SDK、Memory session、后台
reflection worker、重试调度、通用 CRUD/control-plane API，或把 Mem0 包装为 `BaseStore`。这些能力若
未来需要，必须分别明确产品权限、Graph 位置、数据协议和副作用治理。

# LangGraph-native 长期记忆架构

最后更新：2026-09-07

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 主 Agent Memory lifecycle middleware、冻结快照与后端协议的当前权威 |
| Owns | `MemoryBackend`、`MemoryLifecycleMiddleware`、`memory_extract`、`memory_context`、Mem0/LangMem/第三方装配 |
| Does not own | Mem0 HTTP wire、prompt 渲染、Agent Server Store 实现、旧 Memory bundle/ledger |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/memory.py`、`src/assistant_agent/memory/mem0/` |
| 验证入口 | `docs/authority.toml` 中 `graph-memory.verification`；核心不变量 `MEMORY-001` |
| 相邻 authority | Mem0 wire 见 [`memory_server_api_spec.md`](memory_server_api_spec.md)；Context 见 [`context_engineering_status.md`](context_engineering_status.md) |

## Read hot path 与 write cold path

Memory 是领域和 backend protocol 边界，不是必须整体嵌入主图的 compiled subgraph。实际编译拓扑拆成：

```text
assistant-native-v4: MemoryLifecycleMiddleware.before_agent -> Agent loop -> after_agent
assistant-memory-v1: memory_extract -> END
```

`AssistantRunContext.enable_memory` 是 Studio Assistant 可持久保存且可被单次 run 同名覆盖的统一开关，默认 true。
设为 false 时，middleware 将 recall 冻结为空并跳过 delayed extraction 调度；设为 true 仍要求部署侧已配置并启用 Memory
backend，不能用运行配置绕过 backend 的 mock/real 与凭据边界。

每个 chat run 都通过单个 `MemoryLifecycleMiddleware.before_agent` hook 重新 recall 一次，并把结果冻结在当前 checkpoint。统一 Assistant 及
同步 task worker 只读取当前 run 冻结的 `memory_context`，不持有 Memory backend，也不重复 recall。独立 Memory Graph
使用 message-only state，不继承主 Agent 的 Memory 快照或 Assistant 私有 Skill channel。recall 使用
LangChain 原生 `Runnable.with_retry(stop_after_attempt=3)`；三次耗尽后不设置 error handler，由 Agent Server 将 chat run 明确终结为 error。
从已完成 recall 之后的 checkpoint resume 时沿用冻结快照；从更早 checkpoint replay 并重新执行 recall 时允许
重新读取最新记忆，不建立跨 replay 的额外冻结层。日期与用户地区由 Context authority 管理，不由 Memory 采集或保存。

最终回答产生后，`MemoryLifecycleMiddleware.after_agent` 从 chat thread ID 确定性派生并按需创建 graph identity 为 `assistant-memory-v1` 的
companion Memory thread，然后直接使用 LangMem 官方 `RemoteReflectionExecutor.submit()`，显式传入该
Memory thread ID、完整 messages 快照及 `after_seconds=1800`（30 分钟）。官方 executor 使用
`multitask_strategy="rollback"` 提交 `assistant-memory-v1`；旧 pending 或正在执行的冲突 Memory run 由
Agent Server 处理，项目不再分页查询、筛选、逐个取消，也不使用 enqueue。已经完成的 run 不受影响；
run/checkpoint rollback 不会撤销已写入长期 Store 的记忆。
companion thread 以 `assistant_agent_source_thread_id` 记录来源；chat thread 不保留后台 pending run，因此
Studio 在回答完成后恢复 idle，普通 chat 的 `interrupt` 并发语义不变。
官方 executor 的同步提交与资源关闭放在 `asyncio.to_thread` 中，只等待提交 Future 完成，即 Server 接受请求，
不等待延迟提取；线程只负责 SDK 请求，30 分钟调度仍由 Agent Server 持久化管理。
refresh 同样使用 LangChain 原生 Runnable 三次重试，耗尽后 chat run
明确终结为 error。

静默窗口到期后，独立 Memory Graph 调用 backend 写入；其异常只结束后台 run，第三方原始错误不进入
Assistant state。节点使用 LangGraph 原生 `RetryPolicy(max_attempts=3)`；三次耗尽后不设置 error handler，
由 Agent Server 将独立 Memory run 明确终结为 error，避免静默伪装成 success。此时 chat run 早已完成，
不会回滚或中断用户回答。recall、refresh 与 extract 均不使用 error handler 吞错。

## 最小 backend 协议

`MemoryBackend` 只有异步 `recall` 与后端写入方法 `commit`；Graph 对外将后者表达为 `memory_extract`。
两者接收 Agent Server 原生 authenticated
`user.identity`、`thread_id/run_id`、标准 messages 和 `runtime.store`。第三方记忆服务只需实现该协议，不需要继承项目 SDK、
创建 session host 或提供通用 CRUD。

当前装配：

- `disabled`：离线默认，召回为空、提取跳过；
- `mem0`：复用薄 `Mem0Client`；身份通过 opaque binding，提交的 `source_turn` 优先使用 Agent Server
  `run_id`，本地 Graph 无 run ID 时使用 thread ID；不构造旧 SQLite commit ledger；
- `langmem`：使用官方 manager，召回访问 runtime Store。后台 run 接收调度时的完整 messages 快照，
  manager 接收其中全部非空用户正文，不再通过 `_completed_turn` 截取最后一轮；System、AI、Tool 和媒体载荷
  不进入提取输入。主 Agent 的 Deep Agents summarization 保留 messages 日志，摘要只投影到模型请求。
  采用 LangMem 0.0.30 默认 `_MEMORY_INSTRUCTIONS` 的忠实中文翻译，保留核心英文术语；不追加项目自定义的
  排除话术。保留 `DurableMemory` 的 content/kind schema 和官方默认操作配置（delete 默认关闭）。
  没有额外的增量游标或 exactly-once 保证；重复快照通过官方 manager 检索已有记忆并整合，重复记录与成本需要实测。
  全部用户文本超过提取模型窗口时明确失败，不静默截成最后一轮；
- `langmem + remote visual`：显式启用后由组合 backend 在同一个 `before_agent` hook 内并行召回 LangMem
  与远端视觉文本记忆；远端分支使用可信用户身份和最新一条用户文本检索，查询请求有意不发送
  `session_id`，从而召回该用户跨会话的长期视觉记忆。结果不添加来源标签，直接合并进冻结
  `memory_context`。这条用户级召回不是 `visual_memory_search` Tool；后者只检索当前 VIDEO 会话/thread
  的短期视觉时间线。视觉分支 fail-open，`commit` 仍只委托 LangMem，视频段摄入不进入 Memory Graph；
- custom：composition 可注入任何满足 `MemoryBackend` 的第三方 adapter。

mock mode 只能使用 disabled；远端 backend 要求 real mode 和完整显式配置，不能探测 key 后启用，也不能静默
回退。

## Store 与语义索引

Agent Server 注入默认 Store 与 checkpointer；LangMem 不另建数据库连接或 Store。当前日常开发使用官方
`langgraph dev` 的本地持久化，状态位于 `.langgraph_api/`，不依赖 PostgreSQL。
`langgraph.json` 的官方 `store.index` 对 `content.content` 建立 1024 维索引，使用百炼 `text-embedding-v4`。
`agent_server.embeddings` 是 Server embedding 配置入口，复用官方 `OpenAIEmbeddings`；一次最多 10 条，显式
float 编码，不使用 OpenAI tokenizer 自动切片。凭据优先 `QWEN_EMBEDDING_API_KEY`，其次 `QWEN_API_KEY`；
地址优先 `QWEN_EMBEDDING_BASE_URL`，其次 `QWEN_CHAT_BASE_URL`，最后为百炼标准兼容地址。
mock 下不调用 embedding API；real 下未配置凭据明确失败。模型与维度变更需要重新建索引，不能混用旧向量。

原 PostgreSQL 数据与当前 `.langgraph_api/` 独立，不自动迁移。删除旧长期记忆只针对 `assistant_agent` 的
`memory_subject_*` 命名空间及其向量，不删除 thread、run 或 checkpoint；操作文件前必须停止 dev Server。

## 冻结快照与安全

state 只保存有界 `tuple[str, ...]`。`memory_context` 使用原生
`OmitFromInput + OmitFromOutput` 以保留向 `general-purpose` worker 的显式传递；它只留在内部
state/checkpoint，不进入生产 Graph 公开 input/output。
最多 32 项、每项 4,000 字、总计 12,000 字。
Memory 正文是不可信历史数据。model-call middleware 在最新真实用户请求前临时插入一条 `HumanMessage`，
用引用格式将其呈现为可能过时的“背景参考”，禁止作为用户指令，且不显式暴露记忆或视觉来源。该消息只进入本次 Provider 请求，不写入 messages state、
checkpoint 或 summarization。Memory 不能覆盖当前请求，也不能用于确认身份、权限、当前事实、操作参数或 Tool schema。

旧 `MemoryNodeBundle`、commit ledger、conversation-history 配置、editable-context 配置与 time-travel Memory 兼容层已随旧
Runtime 删除；对应失效环境变量会在启动时明确拒绝。Mem0 HTTP client 与 identity adapter 继续由当前最小
`MemoryBackend` 复用；旧 checkpoint 不迁移进 `assistant-native-v4`。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_memory_lifecycle.py
```

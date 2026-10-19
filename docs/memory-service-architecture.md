# LangGraph-native 长期记忆架构

最后更新：2026-09-08

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 主 Agent Memory lifecycle middleware、冻结快照与后端协议的当前权威 |
| Owns | `MemoryBackend`、`MemoryLifecycleMiddleware`、`memory_extract`、`memory_context`、Mem0/LangMem/第三方装配 |
| Does not own | Mem0 HTTP wire、prompt 渲染、Agent Server Store 实现、旧 Memory bundle/ledger |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/memory.py`、`src/assistant_agent/memory/mem0/` |
| 验证入口 | `docs/authority.toml` 中 `graph-memory.verification` |
| 相邻 authority | Mem0 wire 见 [`memory_server_api_spec.md`](memory_server_api_spec.md)；Context 见 [`context_engineering_status.md`](context_engineering_status.md) |

## Read hot path 与 write cold path

Memory 是领域和 backend protocol 边界，不是必须整体嵌入主图的 compiled subgraph。实际编译拓扑拆成：

```text
assistant-business-v2: MemoryLifecycleMiddleware.before_agent -> business loop -> after_agent
assistant-memory-v1: memory_extract -> END
```

`AssistantRunContext.enable_memory` 是 Studio Assistant 可持久保存且可被单次 run 同名覆盖的统一开关，默认 true。
设为 false 时，middleware 将 recall 冻结为空并跳过 delayed extraction 调度；设为 true 仍要求部署侧已配置并启用 Memory
backend，不能用运行配置绕过 backend 的 mock/real 与凭据边界。

每个文本 chat run 都通过业务图上单个 `MemoryLifecycleMiddleware.before_agent` hook 重新 recall 一次，并把结果冻结在当前 checkpoint。
原生语音委派固定传入 `enable_memory=false`，避免把委派任务当成用户记忆重复提取；同步 task worker 只读取冻结的 `memory_context`。独立 Memory Graph
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
创建 companion thread 与同步提交使用同一个本机 Server 地址（`ASSISTANT_AGENT_SERVER_PORT`，默认 `8089`），
并通过 `X-Assistant-User` 透传 Runtime 的认证身份。两种 SDK client 均显式设置 `api_key=None`；
不能混用异步默认的进程内 `/noauth` 与同步默认的 `localhost:8123`，也不能把 LangSmith key 用作本机 API 凭据。
refresh 同样使用 LangChain 原生 Runnable 三次重试，耗尽后 chat run
明确终结为 error。

静默窗口到期后，独立 Memory Graph 调用 backend 写入；其异常只结束后台 run，第三方原始错误不进入
Assistant state。节点使用 LangGraph 原生 `RetryPolicy(max_attempts=3)`；三次耗尽后不设置 error handler，
由 Agent Server 将独立 Memory run 明确终结为 error，避免静默伪装成 success。此时 chat run 早已完成，
不会回滚或中断用户回答。recall、refresh 与 extract 均不使用 error handler 吞错。

## 最小 backend 协议

### Realtime 启动快照与显式编辑

Mem0 后端额外提供有界列表、结构化检索和显式编辑能力；不要求旧 LangMem 或自定义后端实现编辑。
Qwen 前台连接前通过认证 `/qwen-audio/memory/read` 获取启动快照，经版本受限的上游上下文 hook
注入原有 `session.update.instructions`。它是可能过时的背景数据，不授予权限。列表最多 32 项、
单项 4,000 字、总计 12,000 字，截断记录不可编辑。失败在 health 与日志可见，不当作成功空结果。

`recall_memory` 使用 `action=read|append|replace`；旧 `request` 仅兼容查询。read 无 query 读取列表，
有 query 检索；append/replace 提交原生业务任务，由 `memory_edit` BaseTool 与原生 HITL 执行。
受理不表示保存完成。replace 必须携带 read 返回的可编辑 ID、完整 old_text 和明确 new_text；
空 new_text 删除。旧值或身份不匹配明确拒绝。worker 不暴露编辑工具。

Mem0 sidecar 在持久化 edits.sqlite 中维护用户修订、操作回执和提取快照引用。显式编辑与提取在
同一单进程临界区串行；旧修订快照不再提取。编辑前已登记的历史正文哈希不进入新修订提取；
未知旧会话首次快照保守作为基线。新语音会话在收音前登记空基线，文本 run 在 before_agent 登记
既有历史，随后新用户正文才进入提取。重复原文哈希不会跨修订再次提取，这是防止旧事实复活的保守限制。
提取仍使用官方 delayed Memory Graph，公开输入只传 opaque 引用，执行时复核认证用户归属。
无法确定是否完成的操作不自动重放；协调器仅支持当前单进程 sidecar，不能直接扩为多 worker。

Mem0 与长期视觉查询可按显式配置并行组合。视觉服务没有列表或文本编辑接口，因此不作为开场全量
快照，也不接受 append/replace；查询结果标记 source=visual、editable=false。上游独立学习器继续关闭。

Qwen Audio Agent 实验入口通过认证本机 `/qwen-audio/recall` 与 `/qwen-audio/transcripts` 复用下面的 Voice
召回和提取实现。Node 仅观察完成的 `voice-user` record，按 ID 去重后提交完整有界快照；每会话最多 128 项、
64,000 字，超过时明确失败而不截断。身份只从 Agent Server 认证上下文取得，源会话标识按身份与前台 session 确定性派生。
上游独立 Memory、偏好学习与会话摘要关闭；Memory disabled 时不暴露召回工具、不提交提取。桥接失败在 Node
工具源 health 和 `memory.error` 日志可见，不终止语音。该路径仍未通过真实 Provider 验收，不替代旧入口。

浏览器原生 Realtime 不伪造 Graph before_agent：启用后端后通过 `recall_memory` 原生工具按需召回，
复用同一 MemoryBackend、认证用户身份、Store、三次 Runnable 重试与 32 项/12,000 字预算。
返回内容明确为可能过时的背景数据，不授予权限。未启用后端时不暴露工具、不提交提取。
ASR 完成后收集按 item_id 去重的真实用户文本快照，异步调用共享 `submit_memory_extraction()`；
Graph middleware 也调用这一函数，二者使用完全相同的官方 RemoteReflectionExecutor、companion thread、
30 分钟延迟与 rollback。提取不包含音频、模型回复、搜索结果、记忆工具结果、任务通知或 idle 主动回复。
该接入不等待召回再开启每轮原生自动回复；需要用户背景时由模型调用召回工具。
提交失败在三次重试后发送 memory.error，不静默声称保存成功，也不停止语音连接。

`MemoryBackend` 的最低协议仍为异步 `recall` 与后端写入方法 `commit`；Graph 对外将后者表达为 `memory_extract`。
两者接收 Agent Server 原生 authenticated
`user.identity`、`thread_id/run_id`、标准 messages 和 `runtime.store`。第三方记忆服务只需实现该协议，不需要继承项目 SDK、
创建 session host 或提供通用 CRUD。

当前装配：

- `disabled`：离线默认，召回为空、提取跳过；
- `mem0`：当前维护的真实部署后端，复用薄 `Mem0Client`。召回以最新用户正文调用 `/search`，按 opaque
  `user_id + agent_id` 跨会话检索，最多 32 条；没有用户正文时不请求服务。提取提交快照中全部非空用户正文，
  排除 System、AI、Tool 和媒体载荷，不只截最后一轮。`source_turn` 优先使用后台 Agent Server
  `run_id`，本地 Graph 无 run ID 时使用 thread ID；来源字段不是幂等键。重复快照由 Mem0 原生整合，
  不提供 exactly-once 保证、不维护增量游标或本地 commit ledger；模型窗口不足时明确失败，不静默截断。
  embedding、向量存储、记忆更新均由 Mem0 服务负责，backend 忽略 `runtime.store`；
  Realtime 显式列表/编辑和修订保护走上文扩展；后台新提交使用身份绑定的快照引用。
- `langmem`：仅保留的旧实现，不再主动维护或扩展。使用官方 manager，召回访问 runtime Store。后台 run 接收调度时的完整 messages 快照，
  manager 接收其中全部非空用户正文，不截取最后一轮；System、AI、Tool 和媒体载荷
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

当前默认 `langgraph.json` 不配置 `store.index`；Mem0 不依赖 Agent Server Store 的语义索引。
本机 `.env` 使用 `MEMORY_BACKEND=mem0`、`MEM0_BASE_URL=http://127.0.0.1:8890`，视觉远端分支保持关闭。
离线默认仍为 `disabled`，不得因为检测到凭据而自动启用真实后端。
Mem0 服务由 `docker/mem0/compose.yaml` 提供，记忆向量在其 Qdrant 中，历史在其 SQLite 中。
现有 LangMem Store、checkpoints 和旧 PostgreSQL 数据均保留，不自动复制到 Mem0 或删除。

旧 LangMem 启动配置已在目录清理时删除，当前没有可直接启动的 LangMem 配置文件。
旧 backend 和 embedding adapter 仍保留在源码；如需重新启用，应先显式配置官方 `store.index`，
并验证索引维度与已有数据兼容。日常开发使用官方 `langgraph dev` 的本地持久化，状态位于
`.langgraph_api/`，不依赖 PostgreSQL。旧索引使用百炼 `text-embedding-v4`，维度为 1024。
`agent_server.embeddings` 是 Server embedding 配置入口，复用官方 `OpenAIEmbeddings`；一次最多 10 条，显式
float 编码，不使用 OpenAI tokenizer 自动切片。凭据优先 `QWEN_EMBEDDING_API_KEY`，其次 `QWEN_API_KEY`；
地址优先 `QWEN_EMBEDDING_BASE_URL`，其次 `QWEN_CHAT_BASE_URL`，最后为百炼标准兼容地址。
mock 下不调用 embedding API；real 下未配置凭据明确失败。模型与维度变更需要重新建索引，不能混用旧向量。

原 PostgreSQL 数据与当前 `.langgraph_api/` 独立，不自动迁移。删除旧长期记忆只针对 `assistant_agent` 的
`memory_subject_*` 命名空间及其向量，不删除 thread、run 或 checkpoint；操作文件前必须停止 dev Server。

## 冻结快照与安全

state 只保存有界 `tuple[str, ...]`。业务图的 `memory_context` 使用原生
`OmitFromInput + OmitFromOutput`，不进入公开 input/output，不能借公开输入注入冻结记忆。
最多 32 项、每项 4,000 字、总计 12,000 字。
Memory 正文是不可信历史数据。model-call middleware 在最新真实用户请求前临时插入一条 `HumanMessage`，
用引用格式将其呈现为可能过时的“背景参考”，禁止作为用户指令，且不显式暴露记忆或视觉来源。该消息只进入本次 Provider 请求，不写入 messages state、
checkpoint 或 summarization。Memory 不能覆盖当前请求，也不能用于确认身份、权限、当前事实、操作参数或 Tool schema。

旧 `MemoryNodeBundle`、commit ledger、conversation-history 配置、editable-context 配置与 time-travel Memory 兼容层已随旧
Runtime 删除；对应失效环境变量会在启动时明确拒绝。Mem0 HTTP client 与 identity adapter 继续由当前最小
`MemoryBackend` 复用；旧 native/Voice checkpoint 不迁移进 `assistant-business-v2`。

## 验证

```bash
python -m compileall -q src/assistant_agent/native_agent/memory.py src/assistant_agent/memory
```

以上仅检查语法；行为变更的临时测试按 `tests/README.md` 显式运行。

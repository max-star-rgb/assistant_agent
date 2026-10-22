# 文本与视觉长期记忆架构

最后更新：2026-10-20

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Node 原生 Markdown 记忆与保留的 Python Memory backend 权威 |
| Owns | Gateway 文本记忆所有权、Markdown 生命周期、视觉召回边界；Python `MemoryBackend` 与冻结快照 |
| Does not own | Mem0 HTTP wire、prompt 渲染、Agent Server Store 实现、旧 Memory bundle/ledger |
| 源码与 schema 入口 | `integrations/qwen_audio_agent/src/gateway.mjs`、`integrations/qwen_audio_agent/src/config.mjs`、`src/assistant_agent/native_agent/memory.py` |
| 验证入口 | `docs/authority.toml` 中 `graph-memory.verification` |
| 相邻 authority | Mem0 wire 见 [`memory_server_api_spec.md`](memory_server_api_spec.md)；Context 见 [`context_engineering_status.md`](context_engineering_status.md) |

## 当前 Gateway：Node 原生 Markdown

Qwen Audio Gateway 直接使用 qwen-audio-agent 2.0.1 的 MarkdownMemoryProvider、FrontendMemoryRuntime、
原生 `memory` Tool 和 MemorySessionObserver，不再使用 Mem0、Python 文本记忆桥或独立 Memory Graph。
业务仍交给 Agent Server；Node 记忆模块不执行后台业务。

文件位于 `QWEN_AUDIO_STATE_DIR/memory/<SHA-256(ASSISTANT_AGENT_USER)>/USER.md` 与 `MEMORY.md`，
默认状态根为 `~/.local/state/assistant_agent/qwen_audio`。可信部署 identity 决定目录，模型不能指定身份或文件路径。
USER.md 保存长期交互偏好，MEMORY.md 保存长期事实；不迁入旧 Mem0 数据或联调样例，不删除旧数据库。
Gateway 启动时创建尚不存在的空文档（目录权限 0700、文件权限 0600），使用独占创建避免覆盖已有记忆。
空文档不注入模型；内容读写仍由上游原生 Markdown 模块负责，旧 data 目录不会自动迁入。

文本读取直接加载有界文档，不做向量检索。上游 provider 注入预算分别为 6,000 / 8,000 字符，
写入硬上限为对应预算的四倍；超过注入预算时保留完整文件并给出截断提示。上游 runtime 仍施加其规范化预算。
`memory(action=read|append|replace, document=user|memory|all)` 使用原生 schema，all 只用于 read。
精确替换要求 old_text 在已有文档中只出现一次；空 new_text 删除。版本校验、用户写入串行化与原子文件替换
复用上游实现。成功编辑通过原生订阅刷新会话上下文，不委派 `memory_edit` 业务任务。

会话结束后的自动提取复用原生 ConversationSync 新记录和 Markdown patch extractor：至少四条新用户记录，
不设置时间冷却，每个满足条件的新会话批次均可立即提取；同一批记录仍只消费一次，不把恢复的旧历史当新材料。提取失败不阻塞关闭，记录原生失败事件。
显式编辑废弃之前的学习材料与执行中的旧快照。进程崩溃可能丢失尚未提取的记录，不保证 exactly-once。
`QWEN_AUDIO_MEMORY_AUTO=on` 由集成装配；推测偏好学习保持 off。文本模型默认 qwen-flash，可用
`QWEN_AUDIO_MEMORY_MODEL/BASE_URL/API_KEY` 显式配置；默认百炼兼容地址，未单独指定 key 时复用已授权的
DASHSCOPE_API_KEY。mock 不传真实凭据且不启动真实 Gateway；real 仍要求 operator 开关。

主提示词中 `<user_preferences>` 对应 USER.md，明确偏好可覆盖默认人设；观察推断不等同于用户要求。
`<user_memory>` 对应 MEMORY.md，仅作为可能过时的事实背景。两者都不赋予权限，当前请求优先；渲染归 Context authority。

当前本机 `MEMORY_BACKEND=disabled`。Studio/SDK 不读取 Node 文档；业务委派固定 enable_memory=false，
所需背景由前台写入委派请求。保留的旧 Python backend 不参与 Gateway 文本记忆。

## 视觉能力边界

Gateway 在会话初始化时先召回远端视觉快照，此后每成功处理完成 10 个视频段刷新一次。视频沿用独立归档 lane，默认每 30 秒提交一段，断线补交尾段；失败提交不计数，同一进程内最近 4096 个 file_id 去重，进程重启重新计数。
上传复用 Python 窄服务，不经过业务 Agent run。`VisualMemorySnapshots` 通过远端 query 接口以固定查询
“用户近期看到的场景、物品及其位置、活动和重要事件”获取背景，使用可信用户身份跨会话查询；不是全量历史枚举。
每条最多 4000 字、合计最多 12000 字，只保留文本。初始化在连接模型前等待有界召回；后续刷新不阻塞语音，失败保留上一快照，成功空结果清空。
前台通过 `/qwen-audio/visual-memory/snapshot` POST 触发初始化，GET 每 2 秒读取本地快照（不调用远端）；内容变化通过订阅更新原生 Realtime 上下文的 `<visual_memory>`。
快照为进程内用户级状态，不写 Markdown，不提供模型可调用的视觉召回工具，不进入文本提取。
已装配的会话始终保留 `<visual_memory>` 区域，其中仅输出按原顺序以空行分隔的文本，不显示 JSON、字段名、状态或说明文案。内部快照的 `texts` 可为空；`status` 区分 pending、empty、refreshing、ready、ready_empty、disabled、unavailable。状态变化也刷新上下文，失败保留旧文本，空数组不代表已召回到记忆。
计数以远端任务 completed 为准，避免受理后尚未完成处理就提前召回。当前 VIDEO thread 的短期视觉能力与旧 Python hybrid adapter 保持独立。

## 保留的 Python backend（非 Gateway 当前路径）

以下仅适用于显式启用旧 backend 的独立 Python 入口，不是当前 Gateway 记忆方案。

### Read hot path 与 write cold path

Memory 是领域和 backend protocol 边界，不是必须整体嵌入主图的 compiled subgraph。实际编译拓扑拆成：

```text
assistant-business-v2: MemoryLifecycleMiddleware.before_agent -> business loop
Realtime 会话关闭 -> 原生本批用户记录 -> submit_memory_extraction -> assistant-memory-v1
assistant-memory-v1: memory_extract -> END
```

`AssistantRunContext.enable_memory` 是 Studio Assistant 可持久保存且可被单次 run 同名覆盖的统一开关，默认 true。
设为 false 时，middleware 将 recall 冻结为空；业务图不再调度记忆提取。设为 true 仍要求部署侧已配置并启用 Memory
backend，不能用运行配置绕过 backend 的 mock/real 与凭据边界。

每个文本 chat run 都通过业务图上单个 `MemoryLifecycleMiddleware.before_agent` hook 重新 recall 一次，并把结果冻结在当前 checkpoint。
原生语音委派固定传入 `enable_memory=false`，避免把委派任务当成用户记忆重复提取；同步 task worker 只读取冻结的 `memory_context`。独立 Memory Graph
使用 message-only state，不继承主 Agent 的 Memory 快照或 Assistant 私有 Skill channel。recall 使用
LangChain 原生 `Runnable.with_retry(stop_after_attempt=3)`；三次耗尽后不设置 error handler，由 Agent Server 将 chat run 明确终结为 error。
从已完成 recall 之后的 checkpoint resume 时沿用冻结快照；从更早 checkpoint replay 并重新执行 recall 时允许
重新读取最新记忆，不建立跨 replay 的额外冻结层。日期与用户地区由 Context authority 管理，不由 Memory 采集或保存。

前台 Realtime WebSocket 会话关闭后，从身份绑定的源 session ID 确定性派生并按需创建 graph identity 为 `assistant-memory-v1` 的
companion Memory thread，然后直接使用 LangMem 官方 `RemoteReflectionExecutor.submit()`，显式传入该
Memory thread ID、原生待消费用户记录及 `after_seconds=0`（立即执行）。官方 executor 使用
`multitask_strategy="rollback"` 提交 `assistant-memory-v1`；旧 pending 或正在执行的冲突 Memory run 由
Agent Server 处理，项目不再分页查询、筛选、逐个取消，也不使用 enqueue。已经完成的 run 不受影响；
run/checkpoint rollback 不会撤销已写入长期 Store 的记忆。
companion thread 以 `assistant_agent_source_thread_id` 记录来源；业务 thread 不提交后台 pending run，
Studio/SDK 文本业务请求不再自动提取长期记忆，普通 chat 的 `interrupt` 并发语义不变。
官方 executor 的同步提交与资源关闭放在 `asyncio.to_thread` 中，只等待提交 Future 完成，即 Server 接受请求，
不等待提取完成；线程只负责 SDK 请求，后台执行仍由 Agent Server 管理。已移除 `MEMORY_EXTRACTION_DELAY_SECONDS` 配置，旧环境值不再生效。
创建 companion thread 与同步提交使用同一个本机 Server 地址（`ASSISTANT_AGENT_SERVER_PORT`，默认 `8088`），
并通过 `X-Assistant-User` 透传前台会话的认证身份。两种 SDK client 均显式设置 `api_key=None`；
不能混用异步默认的进程内 `/noauth` 与同步默认的 `localhost:8123`，也不能把 LangSmith key 用作本机 API 凭据。
refresh 使用 LangChain 原生 Runnable 三次重试，耗尽后由前台报告 memory.error，不终止语音。
提交调度局部使用 `tracing_context(enabled=False)`，不生成独立 `refresh_memory_extraction` trace；
实际执行的后台 Memory Graph 仍使用原生 tracing。

会话关闭提交后，独立 Memory Graph 立即调度并调用 backend 写入；其异常只结束后台 run，第三方原始错误不进入
Assistant state。节点使用 LangGraph 原生 `RetryPolicy(max_attempts=3)`；三次耗尽后不设置 error handler，
由 Agent Server 将独立 Memory run 明确终结为 error，避免静默伪装成 success。
不会回滚或中断前台回答。recall、refresh 与 extract 均不使用 error handler 吞错。

## 最小 backend 协议

### 独立 Python Realtime 兼容路径

Mem0 的 HTTP 列表、显式编辑和修订保护实现仍保留，wire 见 Memory API authority。
旧 `/qwen-audio/memory/*`、`/qwen-audio/recall`、`/qwen-audio/transcripts` 已删除。

浏览器原生 Realtime 不伪造 Graph before_agent：启用后端后通过 `recall_memory` 原生工具按需召回，
复用同一 MemoryBackend、认证用户身份、Store、三次 Runnable 重试与 32 项/12,000 字预算。
返回内容明确为可能过时的背景数据，不授予权限。未启用后端时不暴露工具、不提交提取。
ASR 完成后仅收集按 item_id 去重的真实用户文本快照，会话关闭时调用共享 `submit_memory_extraction()`；
只有 Realtime 入口调用这一函数，使用官方 RemoteReflectionExecutor、companion thread、
立即执行与 rollback。提取不包含音频、模型回复、搜索结果、记忆工具结果、任务通知或 idle 主动回复。
该接入不等待召回再开启每轮原生自动回复；需要用户背景时由模型调用召回工具。
提交失败在三次重试后发送 memory.error，不静默声称保存成功，也不停止语音连接。

`MemoryBackend` 的最低协议仍为异步 `recall` 与后端写入方法 `commit`；Graph 对外将后者表达为 `memory_extract`。
两者接收 Agent Server 原生 authenticated
`user.identity`、`thread_id/run_id`、标准 messages 和 `runtime.store`。第三方记忆服务只需实现该协议，不需要继承项目 SDK、
创建 session host 或提供通用 CRUD。

当前装配：

- `disabled`：离线默认，召回为空、提取跳过；
- `mem0`：保留的旧部署后端，复用薄 `Mem0Client`。召回以最新用户正文调用 `/search`，按 opaque
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
本机 `.env` 使用 `MEMORY_BACKEND=disabled`；Mem0 地址可能作为旧配置保留，但不被当前部署调用。
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

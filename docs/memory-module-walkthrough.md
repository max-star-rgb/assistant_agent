# 记忆模块新手走读

最后更新：2026-07-14

当前 audit 仍集中在 `MemoryManager` 边界记录；候选是否成为长期记忆由现有
`source_intent`、`MemoryWritePolicy` 和确认流治理，不存在“候选自动升级”的第二条捷径。

Agent，就是“能理解用户请求、选择工具、调用工具并回答的助手程序”。

这份文档面向刚开始参与 Agent 开发的人。它只解释当前 Memory 模块如何工作，不提出新的开发计划。

## 先用生活类比理解 Memory

Memory，就是“长期笔记本”：它不是把每一句聊天都抄下来，而是把以后可能有用、经过筛选、能解释来源、能删除的内容整理成笔记。

session，就是“一次连续对话的编号”：同一个用户今天的一次对话和明天的一次对话，可以是不同 session。

如果用户说了十轮话，ConversationStore 更像“会议录音和临时会议纪要”；Memory 更像“会后整理出来、下次还会用的长期笔记”。

比如：

- 用户说“记住我喜欢深色极简风格”，这适合进入 Memory。
- 用户刚才问“你好”，这只是 ConversationStore 里的会话历史，不适合进入 Memory。
- provider，就是“外部模型或服务供应商”：工具返回一大段 provider 原始响应时，这些原文不能进入 Memory，只能保留安全摘要或引用。

一句话：

```text
ConversationStore 帮系统接上当前会话。
Memory 帮系统在未来任务里记住稳定、有用、可治理的信息。
```

## 先回答几个核心问题

### Memory 和 ConversationStore 有什么区别？

ConversationStore，就是“当前会话记录本”：它保存用户和助手最近说过什么，帮助系统在同一个 session 里接着聊。

Memory，就是“长期笔记本”：它保存跨 session 仍然有用的信息，例如用户偏好、项目事实、任务检查点、商品/产物引用。

两者最大的区别是生命周期：

| 对象 | 人话理解 | 典型内容 | 是否长期复用 |
| --- | --- | --- | --- |
| ConversationStore | 当前会话记录本 | 最近几轮对话、session summary | 主要用于当前 session |
| Memory | 长期笔记本 | 用户偏好、项目事实、任务 checkpoint | 可以跨 session 使用 |

context_summary，就是“当前会话的压缩纪要”：它只服务当前 session，不等于长期 Memory。

对应代码位置：

- `src/assistant_agent/services/assistant_run_service.py`
- `src/assistant_agent/services/session_store.py`
- `src/assistant_agent/memory/manager.py`

### Memory 是什么时候写入的？

写入，就是“把一条信息正式记进长期笔记本”。

当前主要有两种入口：

1. 用户明确要求记住，例如“记住我喜欢深色极简风格”。
2. LLM，也就是“大语言模型助手的大脑”，通过 `memory_save` 工具提出保存动作。

memory candidate，就是“候选笔记”：系统觉得某段信息可能值得记，但还没有真正写入长期笔记本。

MemoryWritePolicy，就是“写入守门员”：它决定候选笔记能不能写、是否要用户确认、保存多久、是否太敏感。

MemoryItem，就是“已经批准并落库的正式笔记”：只有它才算真正的长期 Memory。

默认规则很保守：

- 明确、低敏的用户偏好可以写。
- 高敏信息需要确认。
- API key，就是“访问外部服务的密钥”；token，就是“访问系统或服务的令牌”；raw provider response，就是“供应商返回的未经整理的原文”；base64/raw media，就是“图片、视频、文件等媒体内容的原始数据”；secret，就是“不应该被保存或暴露的敏感秘密”。这些内容会被直接拒绝。
- session `context_summary` 不会自动升级成长期 Memory。
- run summary，就是“一次 Agent 运行结束后的摘要”。普通 run summary 默认只产生候选和审计，不自动写长期 Memory。

对应代码位置：

- `src/assistant_agent/tools/memory_tool.py`
- `src/assistant_agent/memory/write_policy.py`
- `src/assistant_agent/memory/manager.py`
- `MemoryManager.save_explicit_for_identity(...)`
- `MemoryWritePolicy.evaluate_explicit_save(...)`

### Memory 是什么时候被取出来塞进上下文的？

context，就是“LLM 本轮回答前能看到的材料包”：里面可能有当前请求、会话摘要、最近对话、长期记忆、工具观察结果和工具说明。

每次 Agent 运行开始时，系统会先尝试加载相关 Memory。加载出来后，不是全部塞给 LLM，而是经过筛选、分层、预算控制后，放进 `request.metadata["memory_context_*"]`，再交给上下文工程组装成最终 context。

过程可以理解成：

```text
用户这轮说了什么
  -> 系统拿这句话去长期笔记本里找相关笔记
  -> 找到候选记忆
  -> 再筛掉过期、敏感、无权限、被新记忆替代的内容
  -> 按预算整理成 prompt-safe 文本
  -> 放进本轮 context
```

prompt，就是“发给 LLM 的指令和上下文文本”。prompt-safe，就是“可以安全给 LLM 看的版本”：它不能包含 raw provider response、secret、base64/raw media 等危险内容。

对应代码位置：

- `src/assistant_agent/memory/manager.py`
- `src/assistant_agent/memory/context_builder.py`
- `src/assistant_agent/memory/retrieval.py`
- `MemoryManager.load_into_state(...)`
- `MemoryManager.load_context_for_identity(...)`

### Memory 为什么需要 user_id/session_id/project scope 隔离？

isolation，就是“隔离”：A 用户的长期笔记不能被 B 用户看到，项目 A 的记忆不能随便混进项目 B。

`user_id`，就是“这是谁的笔记本”。没有它，系统无法防止跨用户读写。

`session_id`，就是“这次会话是哪一本临时记录”。它帮助区分同一用户不同会话里的上下文。

`project_id`，就是“这条记忆属于哪个项目”。项目级记忆只应该在匹配项目里使用。

tenant，就是“租户或组织边界”：它用于未来多组织隔离。

scope，就是“记忆可见范围”：它告诉系统这条记忆是 session/task/project/user_profile/video/product 等哪类范围。

RequestIdentity，就是“请求身份证”：它把 user、tenant、project、session、allowed scopes 绑在一起，作为读写 Memory 的身份边界。allowed scopes，就是“这次请求被允许看的记忆范围”。

隔离存在的原因很实际：

- 防止跨用户泄漏。
- 防止一个项目的偏好污染另一个项目。
- 防止工具或 LLM 自己传一个假的 user_id 越权读写。
- 让删除、导出、审计都能按用户和范围准确执行。

对应代码位置：

- `src/assistant_agent/schemas/identity.py`
- `src/assistant_agent/services/api_identity.py`
- `src/assistant_agent/memory/manager.py`
- `src/assistant_agent/schemas/memory.py`
- `RequestIdentity`
- `MemoryManager.search_for_identity(...)`
- `MemoryManager.save_explicit_for_identity(...)`

### audit、retention、export、repair 分别解决什么问题？

audit，就是“记账”：记录谁在什么时候读、写、拒绝、删除或导出了什么记忆，方便解释和追责。

retention，就是“定期清理过期记忆”：到期的任务记忆、商品记忆、会话类记忆不能无限期占着长期笔记本。

export，就是“把记忆导出来”：用户、开发者或运维可以查看当前系统到底为某个用户保存了什么。

repair，就是“修复不一致”：比如用户画像和源记忆不一致、旧偏好被新偏好替代后 profile 没同步，就需要检查或重建。

这四个能力解决的是长期系统最常见的实际问题：

| 能力 | 人话问题 | 当前用途 |
| --- | --- | --- |
| audit | “这条记忆为什么会出现？” | 查写入、拒绝、删除、确认、导出等事件 |
| retention | “过期信息会不会一直留着？” | 扫描并删除过期记忆 |
| export | “用户能不能看到系统记了什么？” | 导出用户可见记忆 |
| repair | “数据派生结果错了怎么办？” | 重建 user_profile，报告冲突和 stale source |

对应代码位置：

- `src/assistant_agent/services/memory_audit.py`
- `src/assistant_agent/services/memory_snapshot.py`
- `src/assistant_agent/schemas/memory_audit.py`
- `MemoryAuditService.export_for_identity(...)`
- `MemoryAuditService.sweep_expired_for_identity(...)`
- `MemoryManager.rebuild_user_profile_for_identity(...)`

### typed fact、status 和 conflict 是什么？

typed fact，可以理解成“有固定标签和来源的事实卡片”。除了事实内容，它还记录
`fact_key`、来源、观察时间、版本和冲突策略。status 表示卡片当前是否可用：
`active` 会参与正常召回，`superseded` 已被新事实替代，`disputed` 等待冲突确认，
`retracted` 已撤回。

同一个 `fact_key` 出现不同值时，`MemoryConflictResolver` 先按 `replace`、`coexist`
或 `confirm` 给出确定性决定，再由 `MemoryManager` 修改存储。需要确认的冲突不会把
任一值偷偷当成真相；只有 active fact 才会进入普通 context 和 `user_profile` 投影。

对应代码位置：

- `src/assistant_agent/schemas/memory_intelligence.py`
- `src/assistant_agent/memory/facts.py`
- `src/assistant_agent/memory/conflict_resolver.py`
- `tests/test_memory_fact_status.py`
- `tests/test_memory_manager_fact_conflicts.py`

### SQLite FTS 和 framework mode 分别是什么？

SQLite FTS5 是“快速找候选的目录”。它根据本地文本和中文片段找出可能相关的行，
但 `memory_items` 才是长期记忆真相；候选仍要经过身份、状态、过期、相关性和读取策略
过滤。索引损坏时可以重建，不能反过来把索引内容当权威数据。

`memory_backend=framework` 是显式 opt-in 的另一种生命周期 owner：Hindsight 或 Mem0
运行在隔离 sidecar，项目仍掌握身份、读写策略、确认、prompt safety、治理 ledger/outbox
和工具边界。框架不可用时只允许空结果或显式配置的只读 v2 fallback，不能悄悄把失败
写进本地 v2。选择框架前必须用固定 bake-off gate 比较；没有合格赢家时继续推荐 v2。

运维入口：

- `docs/development/memory-sqlite-operator-runbook.md`
- `docs/development/memory-framework-bakeoff-runbook.md`

## 按用户视角走一遍完整流程

### 1. 用户说话

用户说话后，系统会得到一个 UserRequest。UserRequest，就是“用户这轮请求的结构化表单”：里面有 text、user_id、session_id、图片/视频 ID、metadata 等。metadata，就是“附加信息袋子”，用于放运行时需要的补充字段。

如果用户说：

```text
继续按我喜欢的深色极简风格，帮我生成一个商品展示图。
```

这句话里有两个信号：

- “继续”和“我喜欢的”暗示可能需要查长期 Memory。
- “生成商品展示图”暗示可能要调用工具。

对应代码位置：

- `src/assistant_agent/schemas/requests.py`
- `src/assistant_agent/agent/runtime.py`
- `src/assistant_agent/agent/assistant_loop_nodes.py`

### 2. 系统判断是否要读 Memory

读 Memory，就是“去长期笔记本里找本轮可能相关的笔记”。

系统不是每次都无脑相信 Memory，也不是每次都查出一堆塞给 LLM。当前策略会根据用户请求文本、能力类型、身份范围和检索规则来查。

承接型表达，例如“继续、上次、刚才、之前、这个、那个、同款”，允许 recent fallback。recent fallback，就是“如果关键词没命中，但用户明显在接着上文说，就允许拿最近相关记忆兜底”。

如果用户问一个全新的具体东西，没命中就返回空，不会为了显得聪明乱塞最近记忆。

对应代码位置：

- `src/assistant_agent/memory/retrieval.py`
- `src/assistant_agent/memory/retriever.py`
- `MemoryRetrievalStrategy.retrieve(...)`
- `KeywordMemoryRetriever`

### 3. MemoryManager 检索相关记忆

MemoryManager，就是“记忆管家”：外部调用者想读、写、删、导出、修复记忆，都应该经过它，而不是直接翻底层文件或数据库。

MemoryQuery，就是“检索条件单”：里面写着 user_id、query、session_id、project_id、scope、top_k、是否包含过期记忆等条件。top_k，就是“最多取几条结果”。

MemoryStore，就是“存储柜”：它可以是内存、JSONL 文件或 SQLite 数据库，但对 MemoryManager 暴露同一套读写接口。JSONL，就是“一行一条 JSON 的本地文件格式”；SQLite，就是“本地单文件数据库”。

检索时会做几类过滤：

- 只看当前 user_id 的记忆。
- tenant/project/scope 不匹配的记忆不看。
- 过期记忆默认不看。
- 被 supersede 的旧记忆默认不看。
- session 条件不匹配时不看。

supersede，就是“新笔记替代旧笔记”：例如用户以前喜欢浅色日系，现在明确说喜欢深色极简，旧偏好会被标记为已被新偏好替代。

对应代码位置：

- `src/assistant_agent/memory/manager.py`
- `src/assistant_agent/memory/store.py`
- `src/assistant_agent/memory/jsonl_store.py`
- `src/assistant_agent/memory/sqlite_store.py`
- `MemoryManager.search_for_identity(...)`
- `MemoryStore.search(...)`

### 4. 格式化后进入 context

格式化，就是“把数据库里的记忆整理成 LLM 能读懂的短文本和分层列表”。

MemoryContextBuilder，就是“上下文打包员”：它从检索结果里挑出真正适合放进本轮 context 的 Memory。

它会把记忆分成几类：

| layer | 人话理解 | 例子 |
| --- | --- | --- |
| semantic | 偏好/事实记忆 | 用户喜欢深色极简风格 |
| session | 长期化对话 | 以前保存过的重要对话摘要 |
| episodic | 任务/经历记忆 | 上次比较过某个商品 |
| artifact | 产物/对象引用 | 图片、视频、商品、渲染结果引用 |
| procedural | 过程/规则记忆 | 未来可能加入的操作规则 |

token budget，就是“给记忆占用的字数/词元预算”：Memory 可以很多，但本轮给 LLM 的 context 有上限。

metadata，就是运行时附加信息；如果前面已经读过，可以把它理解成“系统在请求旁边夹的小纸条”。

所以即使检索到 20 条记忆，最终可能只注入 3 条。被丢弃的原因会记录在 metadata 里。

对应代码位置：

- `src/assistant_agent/memory/context_builder.py`
- `src/assistant_agent/memory/manager.py`
- `MemoryContextBuilder.build(...)`
- `MemoryManager.build_context(...)`

### 5. LLM 使用这些记忆回答

LLM，就是“大语言模型”：它不会直接访问数据库，只能看到系统给它放进 context 的内容。

对 LLM 来说，Memory 只是“用户历史数据”，不是系统指令。也就是说，一条 Memory 不能覆盖安全规则、不能绕过工具校验、不能要求系统越权。

比如 Memory 里写着：

```text
用户喜欢深色极简风格。
```

LLM 可以用它来调整回答风格，但不能把它当成“必须调用某个真实 provider”的命令。

对应代码位置：

- `src/assistant_agent/services/context/builder.py`
- `src/assistant_agent/services/context/renderer.py`
- `src/assistant_agent/agent/assistant_loop_nodes.py`

### 6. 需要时产生新的 memory candidate

memory candidate，就是“还没批准的候选笔记”。

候选通常来自两种情况：

- 用户明确说“记住……”。
- 一次任务完成后，系统生成一个安全摘要，认为它可能未来有用。

但候选不是 MemoryItem。候选还没进长期笔记本，必须先过策略。

举例：

```text
用户：记住我喜欢深色极简风格。
```

这会形成一个适合保存的候选。

再比如：

```text
用户：这是我的 API key：...
```

即使用户说“记住”，策略也应该拒绝，因为这是 secret。

对应代码位置：

- `src/assistant_agent/memory/write_policy.py`
- `src/assistant_agent/tools/memory_tool.py`
- `MemoryPromotionCandidate`
- `MemorySaveTool`
- `build_run_summary_promotion_candidate(...)`

### 7. 通过策略后写入 Memory

策略，就是“写入前的规则检查”。

MemoryWritePolicy 会检查：

- 内容是不是空。
- 是否包含 API key、token、bearer credential。
- 是否包含 raw provider response。
- 是否包含 base64/raw media。
- 是否需要用户确认。
- 应该保存成 preference、product、task 等哪种类型。
- 是否设置 TTL，也就是“有效期”。

TTL，就是“保质期”：偏好类记忆通常不过期，任务/商品/产物类记忆通常有默认过期时间。

确认流，就是“先让用户点头再落库”：敏感但可确认的内容不会直接变成 MemoryItem，而是先进入 MemoryPendingConfirmation。

MemoryPendingConfirmation，就是“等待用户确认的草稿”：它只保存脱敏摘要和安全预览，用户确认后才写入正式 Memory。

对应代码位置：

- `src/assistant_agent/memory/write_policy.py`
- `src/assistant_agent/memory/manager.py`
- `src/assistant_agent/schemas/memory_audit.py`
- `MemoryWritePolicy.evaluate_explicit_save(...)`
- `MemoryManager.save_explicit_for_identity(...)`
- `MemoryManager.confirm_memory_for_identity(...)`

### 8. audit 记录整件事

audit，就是“记账”：系统会记录记忆上下文加载、显式保存、拒绝、需要确认、确认/拒绝、promotion decision、删除、导出、retention sweep 等事件。

这不是为了给 LLM 看，而是为了让开发者、用户或运维能回答：

- 为什么这条记忆被写入？
- 为什么这条记忆被拒绝？
- 为什么本轮 context 注入了这些记忆？
- 用户删除后是否还会被检索？
- 过期清理扫到了哪些记忆？

metrics，就是“统计表”：它从 audit events 里汇总计数，例如写入成功多少、拒绝多少、确认多少、删除多少。

snapshot，就是“现场快照”：它把当前 session、conversation、memory context、audit 和 storage 信息拼成一个只读视图，用来排查“本轮到底能看到什么记忆”。

对应代码位置：

- `src/assistant_agent/services/memory_audit.py`
- `src/assistant_agent/services/memory_snapshot.py`
- `src/assistant_agent/schemas/memory_audit.py`
- `src/assistant_agent/schemas/memory_snapshot.py`
- `MemoryManager.record_audit_event(...)`
- `MemoryAuditService.metrics_for_identity(...)`
- `MemorySnapshotService.snapshot_for_identity(...)`

## 当前系统已经能做什么

从新手视角看，当前 Memory 模块已经具备这些基础能力：

- 保存明确的长期记忆。
- 拒绝 secret、raw provider payload、base64/raw media。
- 对敏感记忆走用户确认。
- 按 user/session/project/scope 隔离读写。
- 检索相关长期记忆。
- 把记忆按预算整理进 context。
- 默认不注入过期、敏感、被替代的旧记忆。
- 导出用户记忆。
- 删除用户记忆或 session 记忆。
- 清理过期记忆。
- 重建 user_profile。
- 查看 audit、metrics 和 snapshot。
- 用 typed facts 表达来源、状态和同槽位冲突。
- 用 SQLite FTS5 产生候选，同时保持 canonical row 为真相。
- 显式启用 framework lifecycle owner，并通过 ledger/outbox 和 bake-off gate 治理。

这些能力服务的是“可控长期记忆”，不是“自动无限记住一切”。

对应代码位置：

- `src/assistant_agent/memory/`
- `src/assistant_agent/services/memory_audit.py`
- `src/assistant_agent/services/memory_snapshot.py`
- `tests/test_memory_manager.py`
- `tests/test_memory_store_boundary.py`
- `tests/test_memory_retrieval_eval.py`

## 常见误解

### 误解 1：Memory 就是聊天记录

不是。聊天记录主要在 ConversationStore。Memory 是筛选后的长期笔记。

### 误解 2：检索到的 Memory 都会进入 prompt

不是。检索只是第一步，`MemoryContextBuilder` 还会按安全和预算筛选。

### 误解 3：LLM 可以决定长期保存什么

不是。LLM 可以提出候选，`MemoryWritePolicy` 决定能不能写。

### 误解 4：context_summary 是长期记忆

不是。`context_summary` 是当前 session 的压缩纪要，不自动写入 MemoryStore。

### 误解 5：Memory 可以替代权限控制

不是。Memory 只是上下文数据。identity 是“身份边界”，validator 是“工具调用校验员”，executor 是“真正执行工具的人”，policy 是“规则”，sandbox 是“隔离执行环境”；权限和安全仍由这些边界负责。

对应代码位置：

- `src/assistant_agent/services/context/`
- `src/assistant_agent/memory/manager.py`
- `src/assistant_agent/memory/write_policy.py`
- `src/assistant_agent/tools/memory_tool.py`

## 新手排查入口

### 想知道本轮 LLM 看到了哪些记忆

看 snapshot。snapshot，就是“当前记忆现场快照”。

常用入口：

```text
GET /memory/users/{user_id}/snapshot
```

对应代码位置：

- `src/assistant_agent/services/memory_snapshot.py`
- `src/assistant_agent/schemas/memory_snapshot.py`

### 想知道系统为用户记了什么

看 list/export。export，就是“把记忆导出来给用户或运维查看”。

常用入口：

```text
GET /memory/users/{user_id}/items
GET /memory/users/{user_id}/export
```

对应代码位置：

- `src/assistant_agent/services/memory_audit.py`
- `MemoryAuditService.list_items_for_identity(...)`
- `MemoryAuditService.export_for_identity(...)`

### 想知道为什么某条记忆被写入或拒绝

看 audit events。audit events，就是“记忆操作账本里的单条流水”。

常用入口：

```text
GET /memory/users/{user_id}/events
GET /memory/users/{user_id}/audit
```

对应代码位置：

- `src/assistant_agent/services/memory_audit.py`
- `MemoryAuditService.events_for_identity(...)`
- `MemoryAuditService.audit_for_identity(...)`

### 想知道用户画像是否正确

看 profile status。user_profile，就是“从多条长期记忆合成的一张用户偏好摘要卡”。

常用入口：

```text
GET /memory/users/{user_id}/profile/status
```

对应代码位置：

- `src/assistant_agent/memory/profile.py`
- `src/assistant_agent/memory/manager.py`
- `MemoryManager.rebuild_user_profile_for_identity(...)`

## 简单流程图

```text
用户说话
  |
  v
UserRequest 形成
  |
  v
系统带着 RequestIdentity 准备读 Memory
  |
  v
MemoryManager 用 MemoryQuery 检索 MemoryStore
  |
  v
MemoryRetrievalStrategy 过滤 user/session/project/scope/expired/superseded
  |
  v
MemoryContextBuilder 按安全和预算整理记忆
  |
  v
memory_context_* 写入 request.metadata
  |
  v
AssistantContextPack 组装本轮 context
  |
  v
LLM 基于请求、对话、记忆和工具结果回答或选择工具
  |
  v
如需记住：产生 memory candidate 或调用 memory_save
  |
  v
MemoryWritePolicy 判断 allow / reject / needs confirmation
  |
  +--> reject：不写入，只记录 audit
  |
  +--> needs confirmation：保存 MemoryPendingConfirmation，等用户确认
  |
  +--> allow：生成 MemoryItem，写入 MemoryStore
  |
  v
audit 记录读写、拒绝、确认、删除、导出、清理等事件
```

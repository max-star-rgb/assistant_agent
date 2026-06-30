# 记忆模块负责人走读

最后更新：2026-06-30

这份文档面向项目负责人和技术负责人。它解释当前记忆模块做到了什么、为什么这样分层、哪些风险已经被治理、哪些能力还不该急着做。它不是详细接口文档，也不是历史 roadmap。

当前权威设计入口仍是 `docs/memory-service-architecture.md`。后续开发计划看 `docs/development/memory-kernel-hardening-plan.md`。本文件用于快速建立判断框架。

## 一句话理解

记忆模块负责把“未来有用、经过策略允许、可审计、可删除”的用户历史信息，变成受身份、范围和预算控制的上下文。

它不是：

- 聊天记录数据库。
- 向量 RAG 平台。
- LLM 自由写入的长期用户画像。
- 权限系统或安全策略的替代品。
- 工具调用层的附属小功能。

当前最重要的设计原则是：

```text
LLM proposes.
Policy disposes.
Store persists.
Context engine selects.
Audit explains.
User can delete.
```

LLM 可以提出要记什么，或者通过 `memory_save` 表达候选动作；真正是否写入、写到哪里、是否要确认、何时过期、下次是否能注入上下文，由本地服务和策略决定。

## 当前阶段判断

当前记忆模块已经从“本地 demo 记忆”推进到“Memory Kernel 基础闭环”：

- 有 `MemoryManager` 作为统一服务边界。
- 有 `MemoryWritePolicy` 管写入。
- 有 `MemoryStore` contract 和 InMemory/JSONL/SQLite 三类本地 store。
- 有 SQLite schema version、migration、事务、索引、审计事件、确认队列、备份/恢复和 integrity/index helper。
- 有 `RequestIdentity`，按 user/tenant/project/session/scope 过滤。
- 有 token-aware 的 memory context 注入边界。
- 有显式保存、敏感确认、删除、导出、retention sweep、profile rebuild、snapshot、metrics 和 audit。
- 有 retrieval eval，覆盖误召回、跨用户泄漏、过期/敏感不注入、token budget、superseded preference。

所以当前不是“还没做记忆”的阶段。更准确地说：

```text
本地 Memory Kernel 已经可阶段性收口，
但还不是多用户生产级长期记忆平台。
```

下一步如果继续，不应追求“更聪明”，而应优先补可观测、auth-bound identity、scoped profile、生产备份/恢复演练、上线 UX 和运维闭环。

## 模块边界

记忆模块拥有：

- 长期记忆 item 的存储、读取、删除、导出。
- 检索、排序、过滤、recent fallback。
- prompt-safe memory context 分层和 token budget。
- 显式保存、自动候选、写入策略、TTL、敏感确认。
- compact `user_profile` 维护、冲突/supersedes 处理和 profile rebuild。
- audit event、metrics、snapshot、retention sweep。

记忆模块不拥有：

- session conversation summary。
- prompt-json/native-tool 渲染。
- tool observation compaction。
- 全局 AssistantContextPack 字符预算。
- 真实 provider 调用。
- auth/JWT/session principal。
- ActionValidator、ToolExecutor、sandbox 和权限执行。

最容易混淆的是这三类对象：

| 概念 | 归属 | 生命周期 | 是否长期持久化 |
| --- | --- | --- | --- |
| `context_summary` | context engineering / conversation store | 当前 session 摘要 | 否 |
| `MemoryPromotionCandidate` | memory write policy | 候选长期记忆 | 默认否，只审计 |
| `MemoryItem` | memory service / store | 经过策略允许的长期记忆 | 是 |

判断规则很简单：只有进入 `MemoryStore` 的 `MemoryItem` 才是长期记忆。

## 主链路

一次 Agent 运行中，记忆链路大致是：

```text
UserRequest
  -> AgentGraphRuntime
  -> load_memory node
  -> MemoryManager.load_into_state(...)
  -> MemoryStore.search(MemoryQuery)
  -> MemoryRetrievalStrategy / KeywordMemoryRetriever
  -> MemoryContextBuilder
  -> AgentState.memory_context
  -> request.metadata["memory_context_*"]
  -> AssistantContextPack
  -> prompt/native context
  -> assistant decision
  -> memory_retrieval / memory_save tool when selected
  -> MemoryManager search/save
  -> policy allow / reject / needs confirmation
  -> MemoryStore save or audit-only
  -> save_memory node evaluates promotion candidate when allowed
```

两个关键点：

- 每轮运行开始时，memory context 会被加载成 prompt-safe 数据。
- 长期写入不是“运行结束自动写一段摘要”。默认 automatic promotion 会被策略拒绝，只留下审计候选；真实长期写入优先来自用户显式记忆意图或 `memory_save`。

## 核心组件

| 组件 | 文件 | 负责人视角 |
| --- | --- | --- |
| 服务边界 | `src/multimodal_agent/memory/manager.py` | 统一入口。Agent、API、工具都应该通过它或 service wrapper 操作记忆。 |
| 写入策略 | `src/multimodal_agent/memory/write_policy.py` | 决定能不能写、写到哪里、是否要确认、TTL 和敏感等级。 |
| 存储 contract | `src/multimodal_agent/memory/store.py` | 所有 store 必须实现的行为，包括 memory item 和 confirmation。 |
| JSONL store | `src/multimodal_agent/memory/jsonl_store.py` | 本地/debug 可读持久化，包含 confirmation sidecar。 |
| SQLite store | `src/multimodal_agent/memory/sqlite_store.py` | 当前工程化本地 store，带 schema/migration/audit/confirmation/backup。 |
| 检索策略 | `src/multimodal_agent/memory/retrieval.py` | 本地确定性检索、过滤、fallback、排序。 |
| 关键词检索 | `src/multimodal_agent/memory/retriever.py` | 关键词和中文短片段匹配。 |
| 上下文注入 | `src/multimodal_agent/memory/context_builder.py` | 从检索结果里选出真正注入 prompt 的 memory 子集。 |
| 用户画像 | `src/multimodal_agent/memory/profile.py` | compact `user_profile` 生成和合并逻辑。 |
| 身份边界 | `src/multimodal_agent/schemas/identity.py` | user/tenant/project/session/scope 的访问边界。 |
| 工具适配 | `src/multimodal_agent/tools/memory_tool.py` | Agent 可调用的 `memory_retrieval` / `memory_save`，只能做薄适配。 |
| 审计服务 | `src/multimodal_agent/services/memory_audit.py` | list/get/export/delete/sweep/events/metrics/confirm/profile rebuild。 |
| 快照服务 | `src/multimodal_agent/services/memory_snapshot.py` | 把 session、conversation、memory context、audit、storage 拼成只读快照。 |

负责人判断某个改动该放哪里时，可以先问：

- 是“是否允许写入”？放 `write_policy`。
- 是“查哪些、怎么排序、是否 fallback”？放 retrieval。
- 是“真正进入 prompt 的子集和预算”？放 `context_builder`。
- 是“store schema、事务、迁移、确认持久化”？放 store。
- 是“API 上看、删、导出、审计、确认”？放 `memory_audit` 或 `memory_snapshot`。
- 是“工具输入输出包装”？才放 `tools/memory_tool.py`。

## 存储策略

当前有三类 store：

| store | 用途 | 负责人判断 |
| --- | --- | --- |
| `InMemoryStore` | 单测、本地短生命周期 demo | 保留，不能作为真实持久化。 |
| `JsonlMemoryStore` | 本地可读 debug、轻量持久化 | 保留，但不应承担生产并发和迁移压力。 |
| `SQLiteMemoryStore` | 本地工程化持久化 | 当前重点 store，适合本地长期运行和工程化演练。 |

SQLite 当前承担：

- `memory_items`。
- `memory_audit_events`。
- `memory_confirmations`。
- schema version 和 migration。
- soft delete / hard delete。
- backup / restore / integrity_check / rebuild_indexes。

它仍不是完整多租户生产数据库。当前 tenant/project/scope 过滤主要在服务层，数据库索引还没有完全按未来生产字段展开。

## 读记忆

读路径由 `MemoryQuery` 驱动，基本规则是：

- `user_id` 隔离必做。
- tenant/project/scope/session 可进一步收窄。
- 非空 query 走本地关键词和中文短片段检索。
- 空 query 主要用于浏览、审计、snapshot。
- 只有“继续、上次、刚才、之前、这个、那个、同款”等承接型 query 才允许 recent fallback。
- 过期 memory 默认不返回。
- 被 supersede 的旧 memory 默认不进入主动检索和上下文注入。

被检索到不等于会进入 prompt。`MemoryContextBuilder` 还会执行第二层选择：

- expired 不注入。
- sensitive 不注入。
- 超出 memory token/char budget 的不注入。
- 记录 `memory_context_omitted_count` 和 `memory_context_rejected_reasons`。
- 通过 `memory_context_injected_ids` 标记实际进入上下文的 memory。

负责人需要关注的是：memory store 可以有很多，但每轮 prompt 只能放一点。记忆系统的价值不是“全部塞进去”，而是“受控选择”。

## 写记忆

写入分两类。

### 显式保存

典型来源：

- 用户说“记住我喜欢……”
- assistant 选择 `memory_save` 工具。
- API/service 直接调用 `MemoryManager.save_explicit_for_identity(...)`。

流程：

```text
text/content
  -> MemoryWritePolicy.evaluate_explicit_save(...)
  -> allow / reject / require_user_confirmation
  -> build_explicit_memory_item(...)
  -> MemoryItem validation
  -> duplicate merge / supersedes handling
  -> MemoryStore.save(...)
  -> user_profile update
  -> audit event
```

低敏明确偏好可以直接写。高敏或可确认内容会创建 `MemoryPendingConfirmation`，用户确认后才写入。API key、token、bearer credential、raw provider payload、base64/raw media 即使用户要求记住，也会被拒绝。

### 自动候选

运行结束时可能生成 `MemoryPromotionCandidate`，但默认不写入长期记忆。它主要用于审计和未来可控 promotion。

默认策略：

- `allow_auto_write=False`。
- `allow_long_term_promotion=False`。
- preference/profile memory 需要显式用户意图。
- `context_summary` 不允许自动提升为长期记忆。

这条规则很重要：当前系统宁可少记，也不要乱记。

## 用户画像与 supersedes

`user_profile` 是一个普通 memory item：

```text
memory_id = user_profile
memory_type = preference
source = user_profile
```

它由显式 preference/product/task memory 合并而来，用来给模型提供紧凑画像。

当前 conflict/supersedes 是第一版确定性规则，不做 LLM 语义推断：

- 显式 preference 可带 `content["preference_key"]`。
- 同一治理 scope 下，新 preference 与旧 preference 使用相同 key 且摘要不同，新项 supersede 旧项。
- 旧项写入 `content["superseded_by_memory_id"]`。
- 新项写入 `content["supersedes_memory_ids"]`。
- active retrieval/context/profile 默认排除旧项。
- snapshot debug 可用 `include_superseded=true` 查看链路。

这解决的是“已确认偏好更新后，不再把旧偏好注入 prompt”。它还不是完整的语义冲突系统。

## API 和负责人可见面

当前 API 主要提供：

- list/get memory items。
- audit report。
- events。
- metrics。
- pending confirmations list/confirm/reject。
- profile status/rebuild。
- export。
- snapshot。
- retention sweep。
- item/session delete。

负责人排查时优先看：

- `GET /memory/users/{user_id}/snapshot`
- `GET /memory/users/{user_id}/audit`
- `GET /memory/users/{user_id}/events`
- `GET /memory/users/{user_id}/metrics`
- `GET /memory/users/{user_id}/profile/status`

snapshot 用来回答“本轮会看到哪些记忆”。audit/events 用来回答“为什么写、为什么拒绝、为什么删”。profile/status 用来回答“用户画像是否从源记忆正确生成”。

## 已治理的主要风险

| 风险 | 当前治理 |
| --- | --- |
| 跨用户读取 | `RequestIdentity` + service/retrieval filtering。 |
| body/path user 冒充 | API identity resolver 已集中处理，header auth 仍是 pilot。 |
| raw provider response 入库 | `MemoryItem` validation + write policy 拒绝。 |
| API key/token 入库 | secret-like payload 拒绝。 |
| base64/raw media 入库 | payload key 和 data URI 拒绝。 |
| 自动乱写长期记忆 | automatic promotion 默认 reject。 |
| 敏感记忆直接落库 | `MemoryPendingConfirmation` 确认流。 |
| 过期记忆继续注入 | retrieval/context builder 默认排除。 |
| 旧偏好污染 prompt | superseded memory 默认排除 active context。 |
| 删除不可见 | soft/hard delete、session delete、export、audit 已有本地闭环。 |
| SQLite 损坏或迁移风险 | schema version、newer schema reject、integrity/backup/restore helpers。 |

## 当前限制

这些不是 bug，而是阶段边界：

- 没有真实生产 auth principal；默认 API identity 仍可来自 request/path/query。
- SQLite 是本地工程化 store，不是多租户生产数据库。
- tenant/project/scope 主要在服务层过滤，数据库级索引和约束还不完整。
- 检索是关键词/短片段 baseline，没有 embedding/vector DB。
- `user_profile` 目前是全局用户画像，tenant/project scoped profile 尚未设计。
- supersedes 只处理确定 key 的偏好冲突，不做语义冲突识别。
- external metrics/export 到监控系统还没接。
- confirmation UX 是 API/service 闭环，还不是完整产品体验。

## 不该马上做的事

当前不建议立刻做：

- 默认接 Vector DB。
- 默认接外部 memory service。
- 让 LLM 自动长期写用户画像。
- 把 session `context_summary` 自动升为长期 memory。
- 为了 UI 文案重命名 internal layer 常量。
- 让 Dify/MCP/A2A 直接操作 memory store。
- 把 memory retrieval/ranking 写进 prompt renderer。
- 把 `memory_save` 工具做成真正的 memory owner。

原因不是这些永远不做，而是当前的核心价值在治理边界，不在“更智能地记”。

## 排错入口

### 怀疑记忆没被带进 prompt

看：

- `request.metadata["memory_context_text"]`
- `request.metadata["memory_context_injected_ids"]`
- `request.metadata["memory_context_rejected_reasons"]`
- snapshot 的 `memory_context.blocks`

相关测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_manager.py tests/test_memory_context_builder.py -q
```

### 怀疑检索误召回

看：

- `MemoryQuery.query`
- 是否触发 recent fallback。
- memory type / scope / tenant / project filters。
- retrieval eval 是否需要新增 case。

相关测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_retrieval_strategy.py tests/test_memory_retrieval_eval.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite memory
```

### 怀疑不该写的内容被写了

看：

- `MemoryWritePolicy.evaluate_explicit_save(...)`
- `MemoryWriteDecision`
- audit events 里的 reject/confirmation 记录。
- `MemoryItem` validation 是否覆盖该 payload key。

相关测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_write_policy.py tests/test_memory_privacy_redaction.py tests/test_memory_tool_boundary.py -q
```

### 怀疑用户画像不对

看：

- `GET /memory/users/{user_id}/profile/status`
- `source_memory_ids`
- `superseded_source_memory_ids`
- `profile_conflicts`

相关测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_manager.py tests/test_memory_audit_api.py -q
```

### 怀疑 SQLite 慢或不稳定

先区分：

- 生产/runtime 默认应保留 durable pragmas。
- 测试可以使用显式 fast pragmas。
- 不要因为测试慢就把 runtime 改成不安全默认。

相关测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_store_boundary.py -q
```

## 代码阅读顺序

如果只想理解负责人边界：

1. `docs/memory-service-architecture.md`
2. `docs/development/memory-kernel-hardening-plan.md`
3. `src/multimodal_agent/memory/manager.py`
4. `src/multimodal_agent/memory/write_policy.py`
5. `src/multimodal_agent/memory/store.py`
6. `src/multimodal_agent/memory/retrieval.py`
7. `src/multimodal_agent/memory/context_builder.py`
8. `src/multimodal_agent/services/memory_audit.py`
9. `src/multimodal_agent/services/memory_snapshot.py`
10. `src/multimodal_agent/tools/memory_tool.py`

如果要改上下文注入，再读 `docs/CONTEXT_ENGINEERING_STATUS.md`。如果要改多 Agent 记忆边界，再读 `docs/agent-communication-routing.md`。

## 修改时的负责人检查清单

改 memory 前先确认：

- 默认 mock/local/offline 仍可跑。
- 不引入默认网络依赖。
- 不让工具或 API 直接绕过 `MemoryManager`。
- 不让 model-supplied `user_id` 覆盖 runtime identity。
- 不保存 raw provider/tool/media payload。
- 不把 `context_summary` 当长期 memory。
- 不让 superseded/expired/sensitive memory 默认进入 prompt。
- 新 durable 字段有迁移、测试和文档。
- 新 retrieval 行为有 eval case。
- 删除、导出、audit 能解释新行为。

## 建议的停止点

记忆模块当前已经适合阶段性停止连续开发。以后每轮 memory 工作应按小任务收口：

1. 明确一个治理或能力缺口。
2. 修改代码。
3. 补 focused tests。
4. 更新 `docs/memory-service-architecture.md` 或本走读文档。
5. 跑 memory pytest 和 memory eval。
6. 明确下一步，但不自动继续扩散。

当前最合理的下一组候选任务是：

- auth-bound identity 真正接入生产身份。
- memory health/metrics 面板化。
- scoped user profile 设计。
- SQLite backup/restore drill 和部署 runbook。
- confirmation 的产品 UX。
- retrieval eval corpus 扩展。

这些都属于 Memory Kernel 工程化，不需要先上 Vector DB。

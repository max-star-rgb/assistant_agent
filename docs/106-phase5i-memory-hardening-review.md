# 106 Phase 5I Memory Hardening Review

## 结论

Phase 5I Memory Hardening 已完成。当前 memory 能力仍保持 local-first，不接真实外部 memory service，不接真实 Vector DB，不做复杂 RAG 平台，不做 MCP / Skills。

## 1. Memory Data Model 状态

已整理稳定 schema：

- `MemoryItem`
- `MemoryQuery`
- `MemorySearchResult`

`MemoryItem` 支持 `session_id`、`tags`、`source`、`artifact_refs`、`updated_at`、`expires_at`、`sensitivity`。`memory_type` 覆盖 conversation / preference / product / artifact / task / image / video / generation / render。

Memory 默认只保存摘要、结构化安全字段和 artifact refs，不保存原始媒体、大文件内容、完整 base64 或 provider raw response。

## 2. MemoryStore 边界

已明确 `MemoryStore` 边界：

- `save(item)`
- `search(query)`
- `get(user_id, memory_id)`
- `delete(user_id, memory_id)`
- `delete_by_session(user_id, session_id)`
- `list_by_user(user_id)`
- `clear_user(user_id)`

`InMemoryStore` 和 `JsonlMemoryStore` 均符合该边界。`JsonlMemoryStore` 保留旧式 keyword search 兼容入口，但新代码应使用 `MemoryQuery -> MemorySearchResult`。

## 3. Retrieval Ranking / Context Builder 状态

检索策略支持：

- `top_k`
- `memory_type` filter
- `tag` filter
- `session_id` filter
- `since`
- `include_expired`
- recency ranking
- capability-specific type priority
- artifact ref availability
- context max chars

`memory_context` 会输出短摘要和安全引用，例如 `mock://image/...`，并受 `MemoryQuery.max_context_chars` 限制。Graph runtime 会把 memory summaries / refs 写入 request metadata，供 direct_chat、image_generation、render_3d 等后续能力使用。

## 4. Write Policy / Lifecycle 状态

已新增 `MemoryWritePolicy`，默认策略：

- 自动任务摘要可保存。
- artifact output_ref 可保存为引用。
- raw user text 默认不自动保存。
- raw media 默认不保存。
- sensitive task summary 默认不自动保存。
- preference 默认长期保存。
- task / artifact / product / image / video / render 默认带过期时间。

显式“记住”请求通过 policy 构造 preference / product / task memory，并保留安全摘要。

## 5. Privacy / User Isolation 状态

已覆盖：

- search 按 `user_id` 隔离。
- get/delete/delete_by_session 按 `user_id` 隔离。
- 同名 `memory_id` 不跨用户覆盖或删除。
- Memory payload 写入前脱敏。
- 危险 key 直接拒绝写入。
- `MemoryItem.sensitivity` 支持 normal / private / sensitive。
- trace state summary 不展开完整 memory content。

当前 redaction 复用 Phase 5H provider safety redaction。

## 6. Eval / API / Demo 覆盖

已新增 memory eval suite：

```bash
python scripts/run_evals.py --suite memory
```

覆盖：

- preference memory -> image_generation
- product memory -> render_3d
- task resume
- user isolation
- user-scoped delete

API 当前未新增独立 `/memory/*` endpoint，采用 runtime-level coverage 覆盖 save/search/get/delete。Demo runner 已新增 memory scenarios：

- `memory_to_image_generation`
- `memory_product_to_render`
- `memory_task_resume`
- `memory_user_isolation`

## 7. 默认 Local-First 安全边界

默认路径使用：

- `InMemoryStore`
- `JsonlMemoryStore`
- MockAdapter / LocalJsonAdapter

默认 pytest、eval、demo runner 均离线运行，不调用外部 memory service，不上传 memory，不写入 API Key，不提交真实用户记忆、真实媒体、大文件或 raw provider output。

## 8. 仍然存在的问题

- 仍是轻量 keyword retrieval，不是语义向量检索。
- 没有生产级 memory migration / compaction。
- 没有独立 memory API endpoint，当前仅 runtime-level 覆盖。
- 没有真实多设备/多租户存储后端。
- 没有复杂 PII 分类器，仅做基础 secret/base64/path/provider payload redaction。

这些限制符合 Phase 5I scope。

## 9. Phase 5J 建议

建议进入：

```text
Phase 5J MCP / Skills Packaging
```

前提是继续保持：

- 不让 Skills 直接绕过 Agent planner / validator。
- 不让外部工具绕过 provider safety。
- 不把 memory hardening 扩展成复杂 RAG 平台。

# 105 Phase 5I Memory Hardening Review Checklist

## 必须满足

- MemoryItem / MemoryQuery / MemorySearchResult schema 稳定。
- MemoryStore interface 明确。
- InMemoryStore / JsonlMemoryStore 符合同一边界。
- memory_type 分类清晰。
- memory retrieval 支持 top_k、type filter、session filter、recency。
- memory context builder 有长度限制。
- MemoryWritePolicy 存在。
- 默认不保存 raw media / API Key / provider raw response。
- 显式“记住”请求可写入 memory。
- task / artifact / preference summary 可写入 memory。
- user_id 隔离生效。
- memory search 不跨用户。
- memory delete 不跨用户。
- 敏感信息脱敏生效。
- eval / API / demo 覆盖 memory 场景。
- 默认 pytest / eval / demo 不调用外部 memory service。
- 不接真实 Vector DB。
- 不做复杂 RAG 平台。

## 审计报告

最终生成：

```text
docs/106-phase5i-memory-hardening-review.md
```

报告包含：

1. Memory data model 状态。
2. MemoryStore 边界。
3. Retrieval ranking / context builder 状态。
4. Write policy / lifecycle 状态。
5. Privacy / user isolation 状态。
6. Eval / API / demo 覆盖。
7. 默认 local-first 安全边界。
8. 仍然存在的问题。
9. Phase 5J 建议。

## Phase 5J 建议方向

Phase 5I 后建议考虑：

```text
MCP / Skills Packaging
```

但只有在核心 demo flow、provider safety 和 memory hardening 都稳定后再做。

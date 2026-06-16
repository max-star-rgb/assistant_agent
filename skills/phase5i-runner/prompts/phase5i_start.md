请先阅读 AGENTS.md、docs/99-phase5i-memory-hardening-roadmap.md、tasks/README_PHASE5I.md。

当前进入 Phase 5I：Memory Hardening。

Phase 5I 不新增真实 Provider，不接外部 memory service，不接 Vector DB，不做复杂 RAG 平台，也不做 MCP / Skills。

Phase 5I 只聚焦：

- MemoryItem / MemoryQuery / MemorySearchResult
- MemoryStore boundary
- Retrieval ranking
- Memory context builder
- Memory write policy
- Lifecycle / delete
- Privacy / user isolation
- Memory eval / API / demo coverage
- Phase 5I review

请从 tasks/094-phase5i-memory-hardening-roadmap.md 开始。

本次使用 phase5i-runner skill，允许连续执行到 Task 100 后停止。

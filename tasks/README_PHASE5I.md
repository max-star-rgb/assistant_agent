# Phase 5I Tasks：Memory Hardening

Phase 5I 从 Task 094 开始。该阶段不新增 Provider，不做 MCP / Skills，而是把 memory_retrieval / memory_save 做稳。

## 执行顺序

```text
094 Phase 5I Memory Hardening Roadmap
095 Memory Data Model and Store Boundary
096 Memory Retrieval Ranking and Context Builder
097 Memory Write Policy and Lifecycle
098 Memory Privacy and User Isolation
099 Memory Eval / API / Demo Coverage
100 Phase 5I Review
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 默认使用 InMemoryStore / JsonlMemoryStore。
- 默认测试不得调用外部 memory service。
- 默认 eval / demo 不调用外部 memory service。
- 不自动安装依赖。
- 不写入 API Key。
- 不保存真实用户敏感记忆。
- 不提交真实用户记忆、真实媒体、大文件或 provider raw response。
- 不接真实 Vector DB。
- 不做复杂 RAG 平台。
- 不实现 MCP / Skills。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 默认验收

```bash
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
```

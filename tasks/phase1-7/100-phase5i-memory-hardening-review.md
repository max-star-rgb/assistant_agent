# Task 100 Phase 5I Review

## Goal

生成 Phase 5I 审计报告，确认 Memory Hardening 已完成。

## Read first

- `docs/105-phase5i-memory-hardening-review-checklist.md`
- 当前 docs/
- 当前 tasks/
- 当前 src/
- 当前 tests/
- 当前 scripts/
- 当前 demo_data/

## Requirements

生成：

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

允许小修：

- 更新 README 阶段状态。
- 补充文档链接。
- 更新 `.gitignore`。
- 删除缓存产物。

禁止：

- 接真实 Vector DB。
- 做复杂 RAG 平台。
- 实现 MCP / Skills。
- 写入 API Key。
- 提交真实用户记忆。
- 大规模重构。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_evals.py --suite memory
python scripts/run_demo_flows.py
git status --short
```

## Stop condition

完成后停止，等待用户决定 Phase 5J。

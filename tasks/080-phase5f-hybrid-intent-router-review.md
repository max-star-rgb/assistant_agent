# Task 080 Phase 5F Review

## Goal

生成 Phase 5F 审计报告，确认 Hybrid Intent Router & Planner Quality 已完成。

## Read first

- `docs/82-phase5f-hybrid-intent-router-review-checklist.md`
- 当前 docs/
- 当前 tasks/
- 当前 src/
- 当前 tests/
- 当前 scripts/

## Requirements

生成：

```text
docs/83-phase5f-hybrid-intent-router-review.md
```

报告包含：

1. IntentDecision 状态。
2. Rule Router confidence 状态。
3. CapabilityValidator 状态。
4. LLM Router Adapter 状态。
5. Planner / Slot Filling 状态。
6. Eval comparison 状态。
7. 默认离线安全边界。
8. 仍然存在的问题。
9. Phase 5G 建议。

允许小修：

- 更新 README 阶段状态。
- 补充文档链接。
- 更新 `.gitignore`。
- 删除缓存产物。

禁止：

- 默认调用真实 LLM。
- 接入新的真实 Provider。
- 让 LLM 直接执行工具。
- 实现 MCP / Skills。
- 写入 API Key。
- 大规模重构。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_evals.py --router rule
python scripts/run_evals.py --router mock_llm
python scripts/run_evals.py --router hybrid
git status --short
```

## Stop condition

完成后停止，等待用户决定 Phase 5G。

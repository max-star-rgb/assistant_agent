# Task 093 Phase 5H Review

## Goal

生成 Phase 5H 审计报告，确认 Provider Safety / Retry / Cost / Trace Query 已完成。

## Read first

- `docs/97-phase5h-provider-safety-review-checklist.md`
- 当前 docs/
- 当前 tasks/
- 当前 src/
- 当前 tests/
- 当前 scripts/

## Requirements

生成：

```text
docs/98-phase5h-provider-safety-review.md
```

报告包含：

1. ProviderError taxonomy 状态。
2. SafetyPolicy 状态。
3. Retry / Fallback / Timeout 状态。
4. ProviderCallBudget 状态。
5. Trace Query 状态。
6. Redaction 状态。
7. Eval / API 覆盖。
8. 默认离线安全边界。
9. Phase 5I 建议。

允许小修：

- 更新 README 阶段状态。
- 补充文档链接。
- 更新 `.gitignore`。
- 删除缓存产物。

禁止：

- 新增真实 Provider。
- 默认调用真实 Provider。
- 实现 Memory Hardening。
- 实现 MCP / Skills。
- 写入 API Key。
- 大规模重构。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_evals.py --suite provider_safety
git status --short
```

## Stop condition

完成后停止，等待用户决定 Phase 5I。

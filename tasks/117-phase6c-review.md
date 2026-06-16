# Task 117 Phase 6C Review

## Goal

生成 Phase 6C 审计报告，确认 Real Provider opt-in 文档完成。

## Requirements

生成：

```text
docs/123-phase6c-real-provider-opt-in-review.md
```

报告包含：

1. Provider setup 文档状态。
2. Smoke matrix 状态。
3. 默认 mock/local 边界。
4. API Key 安全状态。
5. Phase 6D 建议。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
git status --short
```

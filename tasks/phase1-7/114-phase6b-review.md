# Task 114 Phase 6B Review

## Goal

生成 Phase 6B 审计报告，确认 API / Web console demo 已完成。

## Requirements

生成：

```text
docs/122-phase6b-api-web-console-review.md
```

报告包含：

1. FastAPI demo 状态。
2. Web console 状态。
3. Trace/run 查询状态。
4. 默认 mock/local 边界。
5. Phase 6C 建议。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
git status --short
```

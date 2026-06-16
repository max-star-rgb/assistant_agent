# Task 122 Release Checklist and Cleanup

## Goal

准备 Phase 6 发布检查。

## Requirements

- 新增或更新 release checklist。
- 检查 `.gitignore`。
- 删除缓存产物。
- 确认没有 API Key。
- 确认没有真实媒体/生成物/渲染产物。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

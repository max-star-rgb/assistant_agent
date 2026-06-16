# Task 123 Phase 6 Review

## Goal

生成 Phase 6 总审计报告。

## Requirements

生成：

```text
docs/121-phase6-productization-review.md
```

报告包含：

1. CLI 状态。
2. API/Web console 状态。
3. Real Provider opt-in 状态。
4. Deployment 状态。
5. Documentation 状态。
6. 安全边界。
7. 剩余问题。
8. 下一阶段建议。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
git status --short
```

## Stop condition

完成后停止。

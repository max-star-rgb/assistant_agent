# Task 040 Phase 4.5 Smoke Test 审计报告

## Goal

完成真实 Vision Provider Smoke Test 准备后，生成阶段审计报告。

## Read first

- `docs/36-phase4-5-real-provider-smoke.md`
- `docs/37-api-key-and-env-safety.md`
- `docs/38-demo-data-and-smoke-flow.md`
- `tasks/038-real-vision-provider-smoke.md`
- `tasks/039-demo-data-and-local-runbook.md`

## Scope

生成：

```text
docs/40-phase4-5-smoke-review.md
```

## Report must include

1. 默认是否仍使用 MockAdapter。
2. 是否存在 `.env.example`。
3. 是否存在 smoke 脚本。
4. smoke 脚本缺 key 时是否清晰退出。
5. 默认 pytest 是否离线。
6. 是否有真实 key 泄露风险。
7. demo_data 是否只包含说明和 `.gitkeep`。
8. 用户下一步如何手动运行真实 Provider。
9. 是否建议进入 Phase 5A。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，等待用户手动真实试跑结果。
